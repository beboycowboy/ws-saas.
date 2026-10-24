from zoneinfo import ZoneInfo


def enrich(rt, notification):
    """Resolve owner-facing content once, independently of the delivery provider."""
    result = dict(notification)
    payload = dict(notification["payload_json"])
    with rt.db.tx(notification["tenant_id"]) as c:
        tenant = c.execute(
            "SELECT name,timezone FROM tenants WHERE id=%s", (notification["tenant_id"],)
        ).fetchone()
        payload["business_name"] = tenant["name"]
        payload["timezone"] = tenant["timezone"]
        if payload.get("appointment_id"):
            row = c.execute(
                """SELECT a.starts_at,a.status,s.name service,u.name customer,u.phone_e164
                FROM appointments a JOIN services s ON s.id=a.service_id JOIN customers u ON u.id=a.customer_id
                WHERE a.id=%s""",
                (payload["appointment_id"],),
            ).fetchone()
            if row:
                payload["appointment"] = {
                    **row,
                    "starts_at": row["starts_at"].astimezone(ZoneInfo(tenant["timezone"])).isoformat(),
                }
        if payload.get("order_id"):
            row = c.execute(
                """SELECT o.status,o.fulfillment,o.total_cents,o.address_note,o.owner_eta_minutes,u.name customer,u.phone_e164
                FROM orders o JOIN customers u ON u.id=o.customer_id WHERE o.id=%s""",
                (payload["order_id"],),
            ).fetchone()
            if row:
                payload["order"] = row
                payload["items"] = c.execute(
                    "SELECT m.name,i.qty,i.unit_price_cents,i.note FROM order_items i JOIN menu_items m ON m.id=i.menu_item_id WHERE i.order_id=%s",
                    (payload["order_id"],),
                ).fetchall()
        if payload.get("conversation_id"):
            row = c.execute(
                "SELECT u.name customer,u.phone_e164 FROM conversations v JOIN customers u ON u.id=v.customer_id WHERE v.id=%s",
                (payload["conversation_id"],),
            ).fetchone()
            if row:
                payload["customer"] = row
    result["payload_json"] = payload
    return result
