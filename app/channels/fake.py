from app.ports import InboundEvent, InboundPayload, SendResult


class FakeChannel:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock

    def parse_inbound(self, request):
        payload = InboundPayload.model_validate_json(request.body)
        if (
            payload.is_group
            or payload.contact_id in {"status@broadcast"}
            or payload.contact_id.endswith("@broadcast")
        ):
            return None
        return InboundEvent(request.account["id"], payload)

    def send_text(self, account, to, text):
        # Worker supplies its durable key via account context, preserving the Channel signature.
        with self.db.tx(account["tenant_id"]) as c:
            c.execute(
                """INSERT INTO fake_channel_log
                (tenant_id,channel_account_id,contact_id,text,delivery_key,created_at)
                VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,delivery_key) DO NOTHING""",
                (account["tenant_id"], account["id"], to, text, account["delivery_key"], self.clock.now()),
            )
        return SendResult(account["delivery_key"], self.clock.now())

    def send_typing(self, account, to):
        return None

    def session_status(self, account):
        return "Connected" if account["status"] == "connected" else "Disconnected"
