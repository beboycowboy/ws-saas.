from pathlib import Path

import pytest
import yaml

from app.admin import demo_tokens
from app.sim import drain, replies, send

SCENARIOS = yaml.safe_load(Path("tests/evals/conversations.yaml").read_text(encoding="utf-8"))


@pytest.mark.integration
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_conversation_final_database_state(rt, scenario):
    slug = scenario["tenant"]
    contact = "eval-" + scenario["id"]
    if "budget" in scenario:
        rt.settings.llm_daily_token_budget_per_tenant = scenario["budget"]
    rt.llm.failure_mode = scenario.get("llm_failure")
    for turn in scenario["turns"]:
        if isinstance(turn, str):
            send(rt, slug, contact, turn)
        else:
            send(rt, slug, contact, turn["text"], kind=turn["kind"])
        drain(rt)
    with rt.db.tx(demo_tokens(rt)[slug]["tenant_id"]) as c:
        customer = c.execute("SELECT * FROM customers WHERE contact_id=%s", (contact,)).fetchone()
        assert customer
        conversation = c.execute(
            "SELECT * FROM conversations WHERE customer_id=%s", (customer["id"],)
        ).fetchone()
        appointments = c.execute(
            "SELECT * FROM appointments WHERE customer_id=%s", (customer["id"],)
        ).fetchall()
        orders = c.execute("SELECT * FROM orders WHERE customer_id=%s", (customer["id"],)).fetchall()
        if "appointments" in scenario:
            assert len(appointments) == scenario["appointments"]
        if "appointment_status" in scenario:
            assert appointments[-1]["status"] == scenario["appointment_status"]
        if "orders" in scenario:
            assert len(orders) == scenario["orders"]
        if "order_status" in scenario:
            assert orders and orders[-1]["status"] == scenario["order_status"]
        if "total_cents" in scenario:
            assert orders[-1]["total_cents"] == scenario["total_cents"]
        if "mode" in scenario:
            assert conversation["mode"] == scenario["mode"]
        if "opted_out" in scenario:
            assert customer["opted_out"] == scenario["opted_out"]
        assert not c.execute("SELECT 1 FROM jobs WHERE status='dead'").fetchone()
    output = replies(rt, slug, contact)
    assert output
    if "replies" in scenario:
        assert len(output) == scenario["replies"]
