import json
import secrets
from datetime import timedelta
from pathlib import Path

import psycopg
import yaml
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import BusinessConfig, cents
from app.core.ingest import token_hash


def admin_connection(settings):
    return psycopg.connect(settings.database_url_admin, row_factory=dict_row, connect_timeout=5)


def load_config(path):
    return BusinessConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def save_tenant(rt, config, update=False):
    with admin_connection(rt.settings) as c:
        prior = c.execute("SELECT id FROM tenants WHERE slug=%s", (config.tenant.slug,)).fetchone()
        if prior and not update:
            raise ValueError("El negocio ya existe; usa tenant update")
        if not prior and update:
            raise ValueError("El negocio no existe; usa tenant create")
        if prior:
            tenant_id = prior["id"]
            c.execute(
                "UPDATE tenants SET name=%s,timezone=%s,config_json=%s,updated_at=%s WHERE id=%s",
                (
                    config.tenant.name,
                    config.tenant.timezone,
                    Jsonb(config.model_dump(mode="json")),
                    rt.clock.now(),
                    tenant_id,
                ),
            )
            c.execute("UPDATE owners SET email=%s WHERE tenant_id=%s", (config.tenant.owner.email, tenant_id))
            c.execute("UPDATE services SET active=false WHERE tenant_id=%s", (tenant_id,))
            c.execute("UPDATE menu_items SET available=false WHERE tenant_id=%s", (tenant_id,))
            tokens = {}
        else:
            tenant_id = c.execute(
                "INSERT INTO tenants(slug,name,timezone,config_json) VALUES(%s,%s,%s,%s) RETURNING id",
                (
                    config.tenant.slug,
                    config.tenant.name,
                    config.tenant.timezone,
                    Jsonb(config.model_dump(mode="json")),
                ),
            ).fetchone()["id"]
            agenda, webhook = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute(
                "INSERT INTO owners(tenant_id,email,agenda_token_hash) VALUES(%s,%s,%s)",
                (tenant_id, config.tenant.owner.email, token_hash(agenda)),
            )
            c.execute(
                "INSERT INTO channel_accounts(tenant_id,provider,webhook_token_hash) VALUES(%s,'fake',%s)",
                (tenant_id, token_hash(webhook)),
            )
            c.execute("INSERT INTO resources(tenant_id,name) VALUES(%s,'Principal')", (tenant_id,))
            tokens = {"agenda_token": agenda, "webhook_token": webhook}
        for s in config.appointments.services:
            c.execute(
                """INSERT INTO services(tenant_id,code,name,minutes,buffer_minutes,price_cents)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,code) DO UPDATE SET
                name=excluded.name,minutes=excluded.minutes,buffer_minutes=excluded.buffer_minutes,
                price_cents=excluded.price_cents,active=true""",
                (tenant_id, s.code, s.name, s.minutes, s.buffer_minutes, cents(s.price_mxn)),
            )
        for i in config.orders.items:
            c.execute(
                """INSERT INTO menu_items(tenant_id,code,name,price_cents,available) VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(tenant_id,code) DO UPDATE SET name=excluded.name,price_cents=excluded.price_cents,
                available=excluded.available""",
                (tenant_id, i.code, i.name, cents(i.price_mxn), i.available),
            )
    return {"tenant_id": str(tenant_id), "slug": config.tenant.slug, **tokens}


def tenant_by_slug(settings, slug):
    with admin_connection(settings) as c:
        row = c.execute("SELECT * FROM tenants WHERE slug=%s", (slug,)).fetchone()
        if not row:
            raise ValueError("Negocio no encontrado")
        return row


def rotate_agenda(rt, slug):
    tenant = tenant_by_slug(rt.settings, slug)
    token = secrets.token_urlsafe(32)
    with rt.db.tx(tenant["id"]) as c:
        c.execute(
            "UPDATE owners SET agenda_token_hash=%s WHERE tenant_id=%s", (token_hash(token), tenant["id"])
        )
    return token


def link_code(rt, slug):
    tenant = tenant_by_slug(rt.settings, slug)
    code = secrets.token_urlsafe(24)
    with rt.db.tx(tenant["id"]) as c:
        c.execute(
            "UPDATE owners SET link_code_hash=%s,link_expires_at=%s WHERE tenant_id=%s",
            (token_hash(code), rt.clock.now() + timedelta(minutes=15), tenant["id"]),
        )
    return code


def bind_owner(rt, code, chat_id):
    owner = rt.db.resolve("link", token_hash(code))
    if not owner:
        raise ValueError("Código inválido")
    with rt.db.tx(owner["tenant_id"]) as c:
        row = c.execute(
            """UPDATE owners SET telegram_chat_id=%s,link_code_hash=NULL,link_expires_at=NULL
            WHERE id=%s AND link_code_hash=%s AND link_expires_at>%s RETURNING id""",
            (chat_id, owner["id"], token_hash(code), rt.clock.now()),
        ).fetchone()
        if not row:
            raise ValueError("Código expirado o utilizado")


def demo_tokens(rt):
    path = rt.settings.runtime_dir / "demo.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def seed_demo(rt):
    from app.core.scheduling import available
    from app.core.tools import Context, execute

    tokens = demo_tokens(rt)
    for path in sorted(Path("examples/tenants").glob("*.yaml")):
        config = load_config(path)
        with admin_connection(rt.settings) as c:
            prior = c.execute("SELECT id FROM tenants WHERE slug=%s", (config.tenant.slug,)).fetchone()
        if prior:
            continue
        created = save_tenant(rt, config)
        tenant_id = created["tenant_id"]
        tokens[config.tenant.slug] = created
        with rt.db.tx(tenant_id) as c:
            customer = c.execute(
                "INSERT INTO customers(tenant_id,contact_id,name,phone_e164) VALUES(%s,'demo-cliente','Cliente Demo','+525500000001') RETURNING id",
                (tenant_id,),
            ).fetchone()
            conv = c.execute(
                "INSERT INTO conversations(tenant_id,customer_id) VALUES(%s,%s) RETURNING id",
                (tenant_id, customer["id"]),
            ).fetchone()
            ctx = Context(c, tenant_id, customer["id"], conv["id"], rt.clock, config, "seed")
            if config.tenant.features.appointments:
                service = c.execute("SELECT * FROM services LIMIT 1").fetchone()
                day = rt.clock.now().date() + timedelta(days=1)
                slots = available(c, config, service, day, day + timedelta(days=6), rt.clock.now())
                if slots:
                    args = {
                        "service_code": service["code"],
                        "starts_at_local": slots[0].isoformat(),
                        "customer_name": "Cliente Demo",
                    }
                    execute(ctx, "preparar_cita", args)
                    ctx.operation_key = "seed-confirm"
                    ctx.confirmation = True
                    execute(ctx, "crear_cita", args)
            if config.tenant.features.orders:
                execute(ctx, "agregar_al_pedido", {"item_code": "taco_pastor", "qty": 5})
                execute(ctx, "preparar_pedido", {"fulfillment": "pickup"})
                ctx.operation_key = "seed-confirm"
                ctx.confirmation = True
                execute(ctx, "confirmar_pedido", {"fulfillment": "pickup"})
    rt.settings.runtime_dir.mkdir(exist_ok=True, parents=True)
    (rt.settings.runtime_dir / "demo.json").write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    return tokens
