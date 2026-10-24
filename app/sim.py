import json
import time
from pathlib import Path
from uuid import uuid4

import yaml

from app.admin import demo_tokens
from app.core.ingest import ingest
from app.worker import tick


def send(rt, slug, contact, text, **overrides):
    token = demo_tokens(rt).get(slug, {}).get("webhook_token")
    if not token:
        raise ValueError("Ejecuta seed demo primero")
    payload = {
        "provider_message_id": str(uuid4()),
        "contact_id": contact,
        "text": text,
        "timestamp": rt.clock.now().isoformat(),
        **overrides,
    }
    return ingest(rt, "fake", token, json.dumps(payload).encode())


def drain(rt):
    # FakeClock makes scripted evals instantaneous; real CLI waits for the same scheduling rules.
    from app.clock import FakeClock

    if isinstance(rt.clock, FakeClock):
        rt.clock.advance(3)
        tick(rt, scheduled=False)
        rt.clock.advance(9)
        tick(rt, scheduled=False)
        return
    deadline = time.monotonic() + 13
    while time.monotonic() < deadline:
        tick(rt, scheduled=False)
        time.sleep(0.2)


def replies(rt, slug, contact):
    tenant = demo_tokens(rt)[slug]
    with rt.db.tx(tenant["tenant_id"]) as c:
        return c.execute(
            "SELECT id,text FROM fake_channel_log WHERE contact_id=%s ORDER BY created_at", (contact,)
        ).fetchall()


def run_script(rt, path):
    script = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    contact = script.get("contact", "script-" + str(uuid4()))
    for turn in script["turns"]:
        send(rt, script["tenant"], contact, turn["say"])
        drain(rt)
        if "expect_status" in turn:
            tenant = demo_tokens(rt)[script["tenant"]]
            with rt.db.tx(tenant["tenant_id"]) as c:
                row = c.execute(
                    "SELECT o.status FROM orders o JOIN customers u ON u.id=o.customer_id WHERE u.contact_id=%s ORDER BY o.created_at DESC LIMIT 1",
                    (contact,),
                ).fetchone()
                assert row and row["status"] == turn["expect_status"], turn
    return replies(rt, script["tenant"], contact)
