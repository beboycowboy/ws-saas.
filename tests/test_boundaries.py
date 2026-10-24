from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from app.ports import LLMResponse
from app.sim import drain, replies, send
from app.worker import work_turn

pytestmark = pytest.mark.integration


def test_llm_context_uses_local_clock_and_omits_contact_details(rt, salon):
    seen = []

    class CapturingLLM:
        def generate(self, messages, *args):
            seen.extend(messages)
            return LLMResponse(text="Hola")

    rt.llm = CapturingLLM()
    send(rt, "salon-ejemplo", "privacy", "hola +525512345678 cliente@example.test")
    drain(rt)
    assert seen[0]["now"].endswith("-06:00")
    assert "+525512345678" not in str(seen)
    assert "cliente@example.test" not in str(seen)
    assert "customer_id" not in str(seen) and "tenant_id" not in str(seen)


def test_customer_messages_do_not_extend_human_handoff(rt, salon):
    send(rt, "salon-ejemplo", "expiry", "humano")
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        expiry = c.execute(
            "SELECT human_until FROM conversations v JOIN customers u ON u.id=v.customer_id WHERE u.contact_id='expiry'"
        ).fetchone()["human_until"]
    rt.clock.advance(3600)
    send(rt, "salon-ejemplo", "expiry", "hola")
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert (
            c.execute(
                "SELECT human_until FROM conversations v JOIN customers u ON u.id=v.customer_id WHERE u.contact_id='expiry'"
            ).fetchone()["human_until"]
            == expiry
        )
    rt.clock.set(expiry)
    send(rt, "salon-ejemplo", "expiry", "hola")
    drain(rt)
    assert len(replies(rt, "salon-ejemplo", "expiry")) == 2


def test_confirmation_before_summary_delivery_cannot_create_booking(rt, salon):
    send(rt, "salon-ejemplo", "early", "agendar corte 2026-10-28T12:00 Ana")
    rt.clock.advance(3)
    work_turn(rt, salon["tenant_id"])
    send(rt, "salon-ejemplo", "early", "confirmar")
    rt.clock.advance(3)
    work_turn(rt, salon["tenant_id"])
    with rt.db.tx(salon["tenant_id"]) as c:
        assert not c.execute(
            "SELECT 1 FROM appointments a JOIN customers u ON u.id=a.customer_id WHERE u.contact_id='early'"
        ).fetchone()
    drain(rt)
    send(rt, "salon-ejemplo", "early", "confirmar")
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert (
            c.execute(
                "SELECT count(*) n FROM appointments a JOIN customers u ON u.id=a.customer_id WHERE u.contact_id='early'"
            ).fetchone()["n"]
            == 1
        )


def test_baja_cancels_response_while_llm_is_running(rt, salon):
    entered, release = Event(), Event()

    class DelayedLLM:
        def generate(self, *args):
            entered.set()
            assert release.wait(5)
            return LLMResponse(text="Respuesta que debe cancelarse")

    rt.llm = DelayedLLM()
    send(rt, "salon-ejemplo", "racing-baja", "hola")
    rt.clock.advance(3)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(work_turn, rt, salon["tenant_id"])
        try:
            assert entered.wait(5)
            send(rt, "salon-ejemplo", "racing-baja", "BAJA")
            with rt.db.tx(salon["tenant_id"]) as c:
                assert c.execute("SELECT opted_out FROM customers WHERE contact_id='racing-baja'").fetchone()[
                    "opted_out"
                ]
        finally:
            release.set()
        future.result(timeout=5)
    drain(rt)
    output = replies(rt, "salon-ejemplo", "racing-baja")
    assert len(output) == 1 and "baja quedó" in output[0]["text"]


def test_malicious_llm_cannot_supply_price_or_identity(rt, tacos, salon):
    class MaliciousLLM:
        def generate(self, *args):
            return LLMResponse(
                tool_calls=[
                    {
                        "name": "agregar_al_pedido",
                        "arguments": {
                            "item_code": "taco_pastor",
                            "qty": 5,
                            "unit_price_cents": 1,
                            "tenant_id": salon["tenant_id"],
                            "customer_id": "other",
                        },
                    }
                ]
            )

    rt.llm = MaliciousLLM()
    send(rt, "taqueria-ejemplo", "malicious", "un pedido")
    drain(rt)
    with rt.db.tx(tacos["tenant_id"]) as c:
        assert not c.execute(
            "SELECT 1 FROM orders o JOIN customers u ON u.id=o.customer_id WHERE u.contact_id='malicious'"
        ).fetchone()
        assert (
            c.execute("SELECT price_cents FROM menu_items WHERE code='taco_pastor'").fetchone()["price_cents"]
            == 1800
        )


def test_other_customer_cancellation_rejected(rt, salon):
    with rt.db.tx(salon["tenant_id"]) as c:
        target = c.execute("SELECT id FROM appointments LIMIT 1").fetchone()["id"]
    send(rt, "salon-ejemplo", "idor", "cancelar cita " + str(target))
    drain(rt)
    send(rt, "salon-ejemplo", "idor", "confirmar")
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert (
            c.execute("SELECT status FROM appointments WHERE id=%s", (target,)).fetchone()["status"]
            == "confirmed"
        )
        assert not c.execute("SELECT 1 FROM outbox WHERE event_key=%s", ("cancel:" + str(target),)).fetchone()


def test_outbox_failure_rolls_back_booking_then_job_retries(rt, salon, monkeypatch):
    import app.core.tools as business_tools

    send(rt, "salon-ejemplo", "atomic", "agendar corte 2026-10-28T12:00 Ana")
    drain(rt)
    original = business_tools.notify

    def fail(*args, **kwargs):
        raise RuntimeError("injected transactional failure")

    monkeypatch.setattr(business_tools, "notify", fail)
    send(rt, "salon-ejemplo", "atomic", "confirmar")
    rt.clock.advance(3)
    work_turn(rt, salon["tenant_id"])
    with rt.db.tx(salon["tenant_id"]) as c:
        assert not c.execute(
            "SELECT 1 FROM appointments a JOIN customers u ON u.id=a.customer_id WHERE u.contact_id='atomic'"
        ).fetchone()
    monkeypatch.setattr(business_tools, "notify", original)
    drain(rt)
    with rt.db.tx(salon["tenant_id"]) as c:
        row = c.execute(
            "SELECT a.id FROM appointments a JOIN customers u ON u.id=a.customer_id WHERE u.contact_id='atomic'"
        ).fetchone()
        assert row
        assert (
            c.execute(
                "SELECT count(*) n FROM outbox WHERE event_key=%s", ("appointment:" + str(row["id"]),)
            ).fetchone()["n"]
            == 1
        )
