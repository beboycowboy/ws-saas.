import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.admin import admin_connection
from app.api import create_app
from app.core.ingest import ingest
from app.sim import drain, replies, send
from app.worker import deliver_outbound, tick, work_turn

pytestmark = pytest.mark.integration


def test_owner_from_phone_silences_bot_and_menu_resumes(rt, salon):
    send(rt, "salon-ejemplo", "owner-echo", "Te atiendo yo", is_from_me=True)
    drain(rt)
    send(rt, "salon-ejemplo", "owner-echo", "hola")
    drain(rt)
    assert replies(rt, "salon-ejemplo", "owner-echo") == []
    send(rt, "salon-ejemplo", "owner-echo", "menú")
    drain(rt)
    assert len(replies(rt, "salon-ejemplo", "owner-echo")) == 1


def test_stale_groups_and_invalid_token(rt, salon):
    payload = {
        "provider_message_id": "ignored",
        "contact_id": "group",
        "text": "hola",
        "timestamp": rt.clock.now().isoformat(),
        "is_group": True,
    }
    assert ingest(rt, "fake", salon["webhook_token"], json.dumps(payload).encode())["status"] == "ignored"
    payload.update(is_group=False, timestamp=(rt.clock.now() - timedelta(minutes=11)).isoformat())
    assert ingest(rt, "fake", salon["webhook_token"], json.dumps(payload).encode())["status"] == "stale"
    with TestClient(create_app(rt)) as client:
        assert client.post("/webhooks/fake/invalid", json=payload).status_code == 404
        assert client.post("/webhooks/fake/invalid", content=b"x" * 20000).status_code == 413


def test_debounce_and_parallel_workers_one_reply(rt, salon):
    for _ in range(3):
        send(rt, "salon-ejemplo", "burst", "hola")
    with rt.db.tx(salon["tenant_id"]) as c:
        assert c.execute("SELECT count(*) n FROM jobs WHERE status='pending'").fetchone()["n"] == 1
    rt.clock.advance(3)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: work_turn(rt, salon["tenant_id"]), range(2)))
    rt.clock.advance(9)
    deliver_outbound(rt, salon["tenant_id"])
    assert len(replies(rt, "salon-ejemplo", "burst")) == 1


def test_crashed_worker_lease_is_recovered(rt, salon):
    send(rt, "salon-ejemplo", "lease", "hola")
    with rt.db.tx(salon["tenant_id"]) as c:
        c.execute(
            "UPDATE jobs SET status='running',locked_at=%s,lock_token=%s",
            (rt.clock.now() - timedelta(seconds=121), uuid4()),
        )
    drain(rt)
    assert len(replies(rt, "salon-ejemplo", "lease")) == 1


def test_outbound_contact_limits_and_stale_suppression(rt, salon):
    rt.settings.rate_limits_contact_hour = 1
    send(rt, "salon-ejemplo", "limited", "hola")
    drain(rt)
    send(rt, "salon-ejemplo", "limited", "hola")
    drain(rt)
    assert len(replies(rt, "salon-ejemplo", "limited")) == 1
    rt.clock.advance(601)
    tick(rt, scheduled=False)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert c.execute("SELECT count(*) n FROM outbound WHERE status='suppressed'").fetchone()["n"] == 1


def test_dev_pages_and_csrf(rt, tacos):
    settings = rt.settings.model_copy(update={"app_env": "dev", "dev_routes": True})
    tick(rt)
    with TestClient(create_app(rt, settings=settings)) as client:
        assert client.get("/dev/inbox").status_code == 200
        response = client.get("/dev/notifications?tenant=taqueria-ejemplo")
        assert response.status_code == 200, response.text
        assert client.post("/dev/chat", json={}).status_code == 403
        token = client.app.state.csrf
        response = client.post(
            "/dev/chat",
            json={"tenant": "taqueria-ejemplo", "text": "hola", "contact": "browser"},
            headers={"X-Dev-CSRF": token},
        )
        assert response.status_code == 200, response.text


def test_agenda_does_not_render_free_notes_and_escapes(rt, salon):
    with admin_connection(rt.settings) as c:
        c.execute(
            "UPDATE customers SET name='<script>alert(1)</script>' WHERE tenant_id=%s", (salon["tenant_id"],)
        )
        c.execute("UPDATE appointments SET notes='PRIVATE-NOTE' WHERE tenant_id=%s", (salon["tenant_id"],))
    with TestClient(create_app(rt)) as client:
        response = client.get("/a/" + salon["agenda_token"])
        assert "PRIVATE-NOTE" not in response.text
        assert "<script>alert(1)</script>" not in response.text
        assert "&lt;script&gt;" in response.text
