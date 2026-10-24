from datetime import timedelta

from psycopg import sql


def purge_tenant(c, now, older_than=90):
    cutoff = now - timedelta(days=older_than)
    result = c.execute(
        "DELETE FROM messages WHERE created_at<%s AND processed_at IS NOT NULL", (cutoff,)
    ).rowcount
    c.execute("DELETE FROM fake_channel_log WHERE created_at<%s", (cutoff,))
    c.execute("DELETE FROM fake_notifications WHERE created_at<%s", (cutoff,))
    c.execute("DELETE FROM outbox WHERE created_at<%s AND status='sent'", (cutoff,))
    c.execute(
        "UPDATE outbox SET payload_json=jsonb_build_object('reason','retention_redacted') WHERE created_at<%s AND status='dead'",
        (cutoff,),
    )
    c.execute("DELETE FROM outbound WHERE created_at<%s AND status<>'pending'", (cutoff,))
    c.execute("DELETE FROM tool_results WHERE created_at<%s", (cutoff,))
    c.execute("DELETE FROM jobs WHERE created_at<%s AND status IN ('done','dead')", (cutoff,))
    c.execute(
        """UPDATE conversations SET state_json=state_json-'pending'
        WHERE state_json ? 'pending' AND (state_json->'pending'->>'expires_at')::timestamptz<%s""",
        (now,),
    )
    return result


def erase_customer(rt, tenant_id, contact):
    """Administrative erasure of one explicitly selected customer's related data."""
    with rt.db.tx(tenant_id) as c:
        customer = c.execute("SELECT id FROM customers WHERE contact_id=%s FOR UPDATE", (contact,)).fetchone()
        if not customer:
            return False
        cid = customer["id"]
        conversations = [
            r["id"] for r in c.execute("SELECT id FROM conversations WHERE customer_id=%s", (cid,))
        ]
        appointments = [
            r["id"] for r in c.execute("SELECT id FROM appointments WHERE customer_id=%s", (cid,))
        ]
        orders = [r["id"] for r in c.execute("SELECT id FROM orders WHERE customer_id=%s", (cid,))]
        notices = [
            r["id"]
            for r in c.execute(
                """SELECT id FROM outbox
            WHERE payload_json->>'conversation_id'=ANY(%s)
            OR payload_json->>'appointment_id'=ANY(%s) OR payload_json->>'order_id'=ANY(%s)""",
                ([str(v) for v in conversations], [str(v) for v in appointments], [str(v) for v in orders]),
            )
        ]
        c.execute("DELETE FROM fake_notifications WHERE outbox_id=ANY(%s)", (notices,))
        c.execute("DELETE FROM outbox WHERE id=ANY(%s)", (notices,))
        for table in ["messages", "jobs", "outbound", "tool_results", "usage_ledger"]:
            c.execute(
                sql.SQL("DELETE FROM {} WHERE conversation_id=ANY(%s)").format(sql.Identifier(table)),
                (conversations,),
            )
        c.execute("DELETE FROM fake_channel_log WHERE contact_id=%s", (contact,))
        c.execute(
            "DELETE FROM audit_events WHERE entity_id=ANY(%s)", (conversations + appointments + orders,)
        )
        c.execute("DELETE FROM appointments WHERE customer_id=%s", (cid,))
        c.execute("DELETE FROM orders WHERE customer_id=%s", (cid,))
        c.execute("DELETE FROM conversations WHERE customer_id=%s", (cid,))
        c.execute("DELETE FROM customers WHERE id=%s", (cid,))
    return True
