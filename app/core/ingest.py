import hashlib
import re
from datetime import timedelta

from psycopg.types.json import Jsonb

from app.errors import BusinessError
from app.ports import InboundRequest


def token_hash(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def sensitive(text):
    # Conservative local safeguard, not a medical-data classifier. Never persist matching text.
    return bool(
        re.search(
            r"\b(diagn[oó]stico|expediente|resultado de laboratorio|mi enfermedad|mi tarjeta|cvv|contrase[nñ]a)\b|(?:\d[ -]?){13,19}",
            text,
            re.I,
        )
    )


def ingest(rt, provider, token, body, headers=None):
    now = rt.clock.now()
    account = rt.db.resolve("channel", token_hash(token))
    if not account or account["provider"] != provider or provider != rt.settings.channel_adapter:
        raise BusinessError("not_found", "Canal no encontrado")
    if len(body) > 16384:
        raise BusinessError("body_too_large", "Cuerpo demasiado grande")
    event = rt.channel.parse_inbound(InboundRequest(account, body, headers or {}))
    if not event:
        return {"status": "ignored"}
    payload = event.payload
    if payload.timestamp < now - timedelta(minutes=10) or payload.timestamp > now + timedelta(minutes=2):
        return {"status": "stale"}
    text = payload.text
    kind = payload.kind
    if sensitive(text):
        text = "[Contenido sensible omitido]"
        kind = "sensitive"
    with rt.db.tx(account["tenant_id"]) as c:
        # Short per-account lock bounds queue growth under concurrent webhook bursts.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("ingest:" + str(account["id"]),))
        if c.execute(
            "SELECT 1 FROM messages WHERE channel_account_id=%s AND provider_message_id=%s",
            (account["id"], payload.provider_message_id),
        ).fetchone():
            return {"status": "duplicate"}
        count = c.execute("SELECT count(*) n FROM jobs WHERE status IN ('pending','running')").fetchone()["n"]
        if count >= rt.settings.queue_max_pending:
            raise BusinessError("queue_full", "Cola llena; reintentar")
        customer = c.execute(
            """INSERT INTO customers(tenant_id,contact_id,phone_e164,created_at)
            VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,contact_id) DO UPDATE
            SET phone_e164=coalesce(customers.phone_e164,excluded.phone_e164) RETURNING *""",
            (account["tenant_id"], payload.contact_id, payload.phone_e164, now),
        ).fetchone()
        conv = c.execute(
            """INSERT INTO conversations(tenant_id,customer_id,last_inbound_at,created_at)
            VALUES(%s,%s,%s,%s) ON CONFLICT(tenant_id,customer_id) DO UPDATE SET last_inbound_at=excluded.last_inbound_at RETURNING *""",
            (account["tenant_id"], customer["id"], now, now),
        ).fetchone()
        msg = c.execute(
            """INSERT INTO messages(tenant_id,conversation_id,channel_account_id,provider_message_id,
            direction,text,kind,provider_timestamp,created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT(channel_account_id,provider_message_id) DO NOTHING RETURNING id""",
            (
                account["tenant_id"],
                conv["id"],
                account["id"],
                payload.provider_message_id,
                "owner" if payload.is_from_me else "in",
                text,
                kind,
                payload.timestamp,
                now,
            ),
        ).fetchone()
        if not msg:
            return {"status": "duplicate"}
        if not payload.is_from_me and text.strip().casefold() == "baja":
            c.execute("UPDATE customers SET opted_out=true WHERE id=%s", (customer["id"],))
            c.execute(
                "UPDATE outbound SET status='suppressed' WHERE customer_id=%s AND status='pending'",
                (customer["id"],),
            )
        if payload.is_from_me:
            config = c.execute(
                "SELECT config_json FROM tenants WHERE id=%s", (account["tenant_id"],)
            ).fetchone()["config_json"]
            c.execute(
                "UPDATE conversations SET mode='human',human_until=%s WHERE id=%s",
                (now + timedelta(hours=config["tenant"]["handoff_hours"]), conv["id"]),
            )
        c.execute(
            """INSERT INTO jobs(tenant_id,conversation_id,type,run_at,first_queued_at,payload_json,created_at)
            VALUES(%s,%s,'turn',%s,%s,%s,%s)
            ON CONFLICT(conversation_id) WHERE type='turn' AND status='pending'
            DO UPDATE SET run_at=least(excluded.run_at,jobs.first_queued_at+interval '10 seconds')""",
            (
                account["tenant_id"],
                conv["id"],
                now + timedelta(seconds=rt.settings.debounce_seconds),
                now,
                Jsonb({}),
                now,
            ),
        )
        c.execute(
            "INSERT INTO usage_ledger(tenant_id,conversation_id,channel_account_id,customer_id,kind,created_at) VALUES(%s,%s,%s,%s,'msg_in',%s)",
            (account["tenant_id"], conv["id"], account["id"], customer["id"], now),
        )
        c.execute("UPDATE channel_accounts SET last_seen_at=%s WHERE id=%s", (now, account["id"]))
    return {"status": "queued", "message_id": str(msg["id"])}
