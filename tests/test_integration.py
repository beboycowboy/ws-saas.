import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.admin import rotate_agenda
from app.api import create_app
from app.core.ingest import ingest
from app.notify.actions import owner_action
from app.sim import drain, replies, send
from app.worker import deliver_outbox, work_turn

pytestmark = pytest.mark.integration


def test_i1_webhook_idempotent_five_deliveries(rt, salon):
    payload = json.dumps(
        {
            "provider_message_id": "same-id",
            "contact_id": "dedup",
            "text": "hola",
            "timestamp": rt.clock.now().isoformat(),
        }
    ).encode()
    for _ in range(5):
        ingest(rt, "fake", salon["webhook_token"], payload)
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert (
            c.execute("SELECT count(*) n FROM messages WHERE provider_message_id='same-id'").fetchone()["n"]
            == 1
        )
        assert (
            c.execute("SELECT count(*) n FROM fake_channel_log WHERE contact_id='dedup'").fetchone()["n"] == 1
        )


def test_i2_twenty_concurrent_bookings_database_exclusion(rt, salon):
    tid = salon["tenant_id"]
    with rt.db.tx(tid) as c:
        resource = c.execute("SELECT id FROM resources").fetchone()["id"]
        service = c.execute("SELECT id FROM services LIMIT 1").fetchone()["id"]
        customer = c.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    start = rt.clock.now() + timedelta(days=3)

    def attempt(index):
        try:
            with rt.db.tx(tid) as c:
                c.execute(
                    """INSERT INTO appointments(tenant_id,resource_id,customer_id,service_id,starts_at,service_ends_at,ends_at,operation_key)
                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s)""",
                    (
                        tid,
                        resource,
                        customer,
                        service,
                        start,
                        start + timedelta(minutes=30),
                        start + timedelta(minutes=35),
                        f"concurrent-{index}",
                    ),
                )
            return True
        except psycopg.errors.ExclusionViolation:
            return False

    with ThreadPoolExecutor(max_workers=20) as pool:
        assert sum(pool.map(attempt, range(20))) == 1


def test_i5_rls_reads_writes_and_context_reset(rt, salon, tacos):
    with rt.db.tx(salon["tenant_id"]) as c:
        assert not c.execute("SELECT * FROM tenants WHERE id=%s", (tacos["tenant_id"],)).fetchall()
        assert (
            c.execute("UPDATE orders SET total_cents=1 WHERE tenant_id=%s", (tacos["tenant_id"],)).rowcount
            == 0
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), c.transaction():
            c.execute(
                "INSERT INTO customers(tenant_id,contact_id) VALUES(%s,'attack')", (tacos["tenant_id"],)
            )
    with rt.db.pool.connection() as c:
        assert c.execute("SELECT count(*) n FROM customers").fetchone()["n"] == 0
        role = c.execute("SELECT rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user").fetchone()
        assert not role["rolsuper"] and not role["rolbypassrls"]


def test_composite_foreign_keys_reject_cross_tenant_objects(rt, salon, tacos):
    with rt.db.tx(tacos["tenant_id"]) as c:
        customer = c.execute("SELECT id FROM customers LIMIT 1").fetchone()["id"]
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with rt.db.tx(salon["tenant_id"]) as c:
            c.execute(
                "INSERT INTO conversations(tenant_id,customer_id) VALUES(%s,%s)",
                (salon["tenant_id"], customer),
            )


def test_i6_webhook_under_500ms_without_llm(rt, salon):
    rt.llm.failure_mode = "timeout"
    with TestClient(create_app(rt)) as client:
        payload = {
            "provider_message_id": "fast",
            "contact_id": "speed",
            "text": "hola",
            "timestamp": rt.clock.now().isoformat(),
        }
        start = time.perf_counter()
        response = client.post("/webhooks/fake/" + salon["webhook_token"], json=payload)
        elapsed = time.perf_counter() - start
        assert response.status_code == 200, response.text
        assert elapsed < 0.5, elapsed


def test_i7_timeout_degraded_and_handoff(rt, salon):
    rt.llm.failure_mode = "timeout"
    send(rt, "salon-ejemplo", "timeout", "hola")
    drain(rt)
    assert "problemas técnicos" in replies(rt, "salon-ejemplo", "timeout")[-1]["text"]
    with rt.db.tx(salon["tenant_id"]) as c:
        row = c.execute(
            "SELECT mode FROM conversations v JOIN customers u ON u.id=v.customer_id WHERE u.contact_id='timeout'"
        ).fetchone()
        assert row["mode"] == "human"


def test_i8_appointment_atomic_outbox_and_retry(rt, salon):
    for text in ["agendar corte 2026-10-28T12:00 Ana", "confirmar"]:
        send(rt, "salon-ejemplo", "booking", text)
        rt.clock.advance(3)
        work_turn(rt, salon["tenant_id"])
        from app.worker import deliver_outbound

        rt.clock.advance(9)
        deliver_outbound(rt, salon["tenant_id"])
    with rt.db.tx(salon["tenant_id"]) as c:
        appointment = c.execute(
            "SELECT a.* FROM appointments a JOIN customers u ON u.id=a.customer_id WHERE u.contact_id='booking'"
        ).fetchone()
        assert appointment
        notice = c.execute(
            "SELECT * FROM outbox WHERE event_key=%s", ("appointment:" + str(appointment["id"]),)
        ).fetchone()
        assert notice and notice["status"] == "pending"
    rt.notifier.failures_left = 1
    deliver_outbox(rt, salon["tenant_id"])
    rt.clock.advance(10)
    deliver_outbox(rt, salon["tenant_id"])
    with rt.db.tx(salon["tenant_id"]) as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM fake_notifications WHERE outbox_id=%s", (notice["id"],)
            ).fetchone()["n"]
            == 1
        )


def test_i3_i4_price_identity_and_explicit_confirmation(rt, tacos):
    for text in ["agregar taco_pastor 5", "pedido delivery Calle 10"]:
        send(rt, "taqueria-ejemplo", "prices", text)
        drain(rt)
    with rt.db.tx(tacos["tenant_id"]) as c:
        row = c.execute(
            "SELECT o.* FROM orders o JOIN customers u ON u.id=o.customer_id WHERE u.contact_id='prices'"
        ).fetchone()
        assert row["status"] == "draft"
    send(rt, "taqueria-ejemplo", "prices", "confirmar")
    drain(rt)
    with rt.db.tx(tacos["tenant_id"]) as c:
        row = c.execute("SELECT * FROM orders WHERE id=%s", (row["id"],)).fetchone()
        assert row["status"] == "placed" and row["total_cents"] == 12000


def test_owner_actions_idempotent_and_invalid_transitions(rt, tacos):
    with rt.db.tx(tacos["tenant_id"]) as c:
        order = c.execute("SELECT id FROM orders WHERE status='placed'").fetchone()["id"]
        owner = c.execute("SELECT id FROM owners").fetchone()["id"]
    for _ in range(2):
        owner_action(rt, tacos["tenant_id"], owner, "accept", order)
    from app.errors import BusinessError

    with pytest.raises(BusinessError):
        owner_action(rt, tacos["tenant_id"], owner, "reject", order)
    owner_action(rt, tacos["tenant_id"], owner, "eta", order, 30)
    with rt.db.tx(tacos["tenant_id"]) as c:
        assert (
            c.execute("SELECT owner_eta_minutes FROM orders WHERE id=%s", (order,)).fetchone()[
                "owner_eta_minutes"
            ]
            == 30
        )


def test_agenda_revocation_headers_escape_and_dev_absent(rt, salon):
    with TestClient(create_app(rt)) as client:
        response = client.get("/a/" + salon["agenda_token"])
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert client.get("/dev/inbox").status_code == 404
        token = rotate_agenda(rt, "salon-ejemplo")
        assert client.get("/a/" + salon["agenda_token"]).status_code == 404
        assert client.get("/a/" + token).status_code == 200


def test_baja_suppresses_pending_and_future(rt, salon):
    send(rt, "salon-ejemplo", "optout", "hola")
    rt.clock.advance(3)
    work_turn(rt, salon["tenant_id"])
    send(rt, "salon-ejemplo", "optout", "BAJA")
    drain(rt)
    send(rt, "salon-ejemplo", "optout", "hola otra vez")
    drain(rt)
    output = replies(rt, "salon-ejemplo", "optout")
    assert len(output) == 1 and "baja quedó" in output[0]["text"]


def test_sensitive_payload_not_persisted(rt, salon):
    send(rt, "salon-ejemplo", "sensitive", "mi diagnóstico es secreto")
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert not c.execute("SELECT 1 FROM messages WHERE text LIKE '%es secreto%'").fetchone()
    assert "no envíes" in replies(rt, "salon-ejemplo", "sensitive")[-1]["text"]
