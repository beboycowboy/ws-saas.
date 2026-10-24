import json
import logging
from datetime import timedelta
from pathlib import Path

import typer

from app.admin import (
    admin_connection,
    link_code,
    load_config,
    rotate_agenda,
    save_tenant,
    seed_demo,
    tenant_by_slug,
)
from app.db import migrate
from app.runtime import Runtime
from app.settings import Settings

cli = typer.Typer(no_args_is_help=True, help="Barrio Bot · MVP local de citas y pedidos")
tenant_cli = typer.Typer(no_args_is_help=True)
owner_cli = typer.Typer(no_args_is_help=True)
channel_cli = typer.Typer(no_args_is_help=True)
usage_cli = typer.Typer(no_args_is_help=True)
conversation_cli = typer.Typer(no_args_is_help=True)
customer_cli = typer.Typer(no_args_is_help=True)
backup_cli = typer.Typer(no_args_is_help=True)
sim_cli = typer.Typer(no_args_is_help=True)
seed_cli = typer.Typer(no_args_is_help=True)
for name, group in [
    ("tenant", tenant_cli),
    ("owner", owner_cli),
    ("channel", channel_cli),
    ("usage", usage_cli),
    ("conversation", conversation_cli),
    ("customer", customer_cli),
    ("backup", backup_cli),
    ("sim", sim_cli),
    ("seed", seed_cli),
]:
    group.callback()(lambda: None)
    cli.add_typer(group, name=name)


def output(value):
    typer.echo(json.dumps(value, default=str, ensure_ascii=False, indent=2))


def runtime():
    settings = Settings()
    migrate(settings)
    return Runtime(settings)


@cli.command("migrate")
def migration():
    migrate(Settings())
    typer.echo("Migraciones aplicadas")


@cli.command()
def api(host: str = "127.0.0.1", port: int = 8000):
    import uvicorn

    from app.api import create_app

    settings = Settings()
    migrate(settings)
    uvicorn.run(create_app(settings=settings), host=host, port=port, access_log=False)


@cli.command()
def worker():
    from app.worker import run

    rt = runtime()
    logging.basicConfig(level=rt.settings.log_level, format="%(message)s")
    try:
        run(rt)
    finally:
        rt.close()


@seed_cli.command("demo")
def seed():
    rt = runtime()
    try:
        tokens = seed_demo(rt)
        output(
            {
                slug: {"agenda": rt.settings.public_base_url + "/a/" + data["agenda_token"]}
                for slug, data in tokens.items()
            }
        )
    finally:
        rt.close()


@tenant_cli.command("create")
def tenant_create(config: Path = typer.Option(..., exists=True)):
    rt = runtime()
    try:
        output(save_tenant(rt, load_config(config)))
    finally:
        rt.close()


@tenant_cli.command("update")
def tenant_update(config: Path = typer.Option(..., exists=True)):
    rt = runtime()
    try:
        output(save_tenant(rt, load_config(config), update=True))
    finally:
        rt.close()


@tenant_cli.command("list")
def tenant_list():
    with admin_connection(Settings()) as c:
        output(c.execute("SELECT id,slug,name,status FROM tenants ORDER BY slug").fetchall())


@owner_cli.command("rotate-agenda-token")
def rotate(tenant: str = typer.Option(...)):
    rt = runtime()
    try:
        token = rotate_agenda(rt, tenant)
        output({"agenda": rt.settings.public_base_url + "/a/" + token})
    finally:
        rt.close()


@owner_cli.command("link-code")
def owner_link(tenant: str = typer.Option(...)):
    rt = runtime()
    try:
        output(
            {
                "code": link_code(rt, tenant),
                "expires_in_minutes": 15,
                "note": "Configura el bot de Telegram para construir su enlace profundo",
            }
        )
    finally:
        rt.close()


@channel_cli.command("add")
def channel_add(tenant: str = typer.Option(...), provider: str = "wasender"):
    import secrets

    from app.core.ingest import token_hash

    if provider not in {"wasender", "fake"}:
        raise typer.BadParameter("provider debe ser fake o wasender")
    rt = runtime()
    try:
        row = tenant_by_slug(rt.settings, tenant)
        token = secrets.token_urlsafe(32)
        with rt.db.tx(row["id"]) as c:
            c.execute(
                "INSERT INTO channel_accounts(tenant_id,provider,webhook_token_hash,status) VALUES(%s,%s,%s,'unknown')",
                (row["id"], provider, token_hash(token)),
            )
        output(
            {
                "webhook": f"{rt.settings.public_base_url}/webhooks/{provider}/{token}",
                "note": "Adaptador real aún no conectado",
            }
        )
    finally:
        rt.close()


def duration(value):
    if not value.endswith("d") or not value[:-1].isdigit() or int(value[:-1]) < 1:
        raise typer.BadParameter("Usa un número positivo de días, por ejemplo 7d")
    return timedelta(days=int(value[:-1]))


@usage_cli.command("report")
def usage(since: str = "7d"):
    rt = runtime()
    try:
        with admin_connection(rt.settings) as c:
            output(
                c.execute(
                    """SELECT t.slug,u.conversation_id,u.kind,count(*) events,sum(u.tokens_in+u.tokens_out) tokens,
                sum(u.est_cost_micro_usd) cost_micro_usd FROM usage_ledger u JOIN tenants t ON t.id=u.tenant_id
                WHERE u.created_at>=%s GROUP BY t.slug,u.conversation_id,u.kind ORDER BY t.slug""",
                    (rt.clock.now() - duration(since),),
                ).fetchall()
            )
    finally:
        rt.close()


@conversation_cli.command("export")
def export(
    tenant: str = typer.Option(...),
    since: str = "7d",
    destination: Path = Path(".runtime/conversations.json"),
):
    rt = runtime()
    try:
        row = tenant_by_slug(rt.settings, tenant)
        with rt.db.tx(row["id"]) as c:
            rows = c.execute(
                "SELECT conversation_id,direction,text,created_at FROM messages WHERE created_at>=%s ORDER BY received_seq",
                (rt.clock.now() - duration(since),),
            ).fetchall()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(rows, default=str, ensure_ascii=False, indent=2), encoding="utf-8")
        typer.echo(str(destination.resolve()))
    finally:
        rt.close()


@cli.command()
def purge(older_than: str = "90d"):
    from app.maintenance import purge_tenant

    rt = runtime()
    try:
        total = 0
        for tenant_id in rt.db.tenant_ids():
            with rt.db.tx(tenant_id) as c:
                total += purge_tenant(c, rt.clock.now(), duration(older_than).days)
        output({"messages_deleted": total})
    finally:
        rt.close()


@customer_cli.command("erase")
def customer_erase(
    tenant: str = typer.Option(...),
    contact: str = typer.Option(...),
    confirm_contact: str = typer.Option(...),
):
    from app.maintenance import erase_customer

    if contact != confirm_contact:
        raise typer.BadParameter("--confirm-contact debe coincidir exactamente con --contact")
    rt = runtime()
    try:
        row = tenant_by_slug(rt.settings, tenant)
        output({"erased": erase_customer(rt, row["id"], contact)})
    finally:
        rt.close()


@backup_cli.command("create")
def backup_create():
    from app.backup.service import create_backup

    rt = runtime()
    try:
        typer.echo(create_backup(rt))
    finally:
        rt.close()


@backup_cli.command("restore")
def backup_restore(name: str, destination_url: str = typer.Option(...)):
    from app.backup.service import restore_backup

    rt = runtime()
    try:
        restore_backup(rt, name, destination_url)
        typer.echo("Backup restaurado en la base de destino")
    finally:
        rt.close()


@sim_cli.command("chat")
def chat(tenant: str = typer.Option(...), contact: str = typer.Option("cliente-demo", "--as")):
    from app.sim import drain, replies, send

    rt = runtime()
    seen = set()
    try:
        typer.echo("Simulador local. Escribe salir para terminar.")
        while True:
            text = typer.prompt("Tú")
            if text.casefold() == "salir":
                break
            send(rt, tenant, contact, text)
            drain(rt)
            for reply in replies(rt, tenant, contact):
                if reply["id"] not in seen:
                    typer.echo("Bot: " + reply["text"])
                    seen.add(reply["id"])
    finally:
        rt.close()


@sim_cli.command("run")
def script(file: Path = typer.Option(..., exists=True)):
    from app.sim import run_script

    rt = runtime()
    try:
        output(run_script(rt, file))
    finally:
        rt.close()


def main():
    cli()


def sim_main():
    sim_cli()


def seed_main():
    seed_cli()


if __name__ == "__main__":
    main()
