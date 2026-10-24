from datetime import timedelta
from uuid import UUID

from app.core.conversation import enqueue_reply
from app.errors import BusinessError


def owner_action(rt, tenant_id, owner_id, action, entity_id, eta=None):
    if action not in {"accept", "reject", "take", "release", "eta"}:
        raise BusinessError("invalid_action", "Acción inválida")
    entity_id = UUID(str(entity_id))
    with rt.db.tx(tenant_id) as c:
        owner = c.execute("SELECT id FROM owners WHERE id=%s", (owner_id,)).fetchone()
        if not owner:
            raise BusinessError("not_found", "Dueño no encontrado")
        if action in {"take", "release"}:
            conv = c.execute("SELECT * FROM conversations WHERE id=%s FOR UPDATE", (entity_id,)).fetchone()
            if not conv:
                raise BusinessError("not_found", "Conversación no encontrada")
            hours = c.execute("SELECT config_json FROM tenants WHERE id=%s", (tenant_id,)).fetchone()[
                "config_json"
            ]["tenant"]["handoff_hours"]
            c.execute(
                "UPDATE conversations SET mode=%s,human_until=%s WHERE id=%s",
                (
                    "human" if action == "take" else "bot",
                    rt.clock.now() + timedelta(hours=hours) if action == "take" else None,
                    entity_id,
                ),
            )
            entity = "conversation"
        else:
            row = c.execute("SELECT * FROM orders WHERE id=%s FOR UPDATE", (entity_id,)).fetchone()
            if not row:
                raise BusinessError("not_found", "Pedido no encontrado")
            target = {"accept": "accepted", "reject": "rejected", "eta": "accepted"}[action]
            if action == "eta":
                if eta not in {15, 30, 45} or row["status"] != "accepted":
                    raise BusinessError(
                        "invalid_transition", "ETA requiere pedido aceptado y 15, 30 o 45 minutos"
                    )
                c.execute("UPDATE orders SET owner_eta_minutes=%s WHERE id=%s", (eta, entity_id))
            elif row["status"] == target:
                return {"status": target}
            elif row["status"] != "placed":
                raise BusinessError("invalid_transition", "El pedido ya no está pendiente")
            else:
                c.execute("UPDATE orders SET status=%s,version=version+1 WHERE id=%s", (target, entity_id))
                conv = c.execute(
                    "SELECT * FROM conversations WHERE customer_id=%s", (row["customer_id"],)
                ).fetchone()
                if (
                    rt.settings.feature_owner_triggered_replies
                    and conv
                    and conv["last_inbound_at"]
                    and conv["last_inbound_at"] >= rt.clock.now() - timedelta(minutes=10)
                ):
                    account = c.execute(
                        "SELECT channel_account_id FROM messages WHERE conversation_id=%s AND direction='in' ORDER BY received_seq DESC LIMIT 1",
                        (conv["id"],),
                    ).fetchone()
                    if account:
                        enqueue_reply(
                            rt,
                            c,
                            tenant_id,
                            conv,
                            account["channel_account_id"],
                            f"owner:{entity_id}:{target}",
                            "Tu pedido fue aceptado."
                            if action == "accept"
                            else "El negocio no pudo aceptar tu pedido.",
                            allow_human=True,
                        )
            entity = "order"
        c.execute(
            "INSERT INTO audit_events(tenant_id,actor,action,entity,entity_id) VALUES(%s,%s,%s,%s,%s)",
            (tenant_id, "owner:" + str(owner_id), action, entity, entity_id),
        )
    return {"status": "ok"}
