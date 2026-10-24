import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from psycopg.errors import ExclusionViolation
from psycopg.types.json import Jsonb
from pydantic import Field

from app.config import BusinessConfig, StrictModel, cents
from app.core.scheduling import DAYS, available, candidates, local_instant
from app.errors import BusinessError


class Empty(StrictModel):
    pass


class Availability(StrictModel):
    service_code: str
    date_from: date
    date_to: date


class Appointment(StrictModel):
    service_code: str
    starts_at_local: str
    customer_name: str = Field(default="Cliente", min_length=1, max_length=100)


class CancelAppointment(StrictModel):
    appointment_id: UUID


class AddItem(StrictModel):
    item_code: str
    qty: int = Field(ge=1, le=99, strict=True)
    note: str = Field(default="", max_length=300)


class RemoveItem(StrictModel):
    item_code: str
    qty: int | None = Field(default=None, ge=1, le=99, strict=True)


class PlaceOrder(StrictModel):
    fulfillment: Literal["pickup", "delivery"]
    address_note: str = Field(default="", max_length=300)
    customer_name: str = Field(default="Cliente", min_length=1, max_length=100)


class Handoff(StrictModel):
    reason: str = Field(default="Solicitud del cliente", max_length=200)


SCHEMAS = {
    "consultar_disponibilidad": Availability,
    "preparar_cita": Appointment,
    "crear_cita": Appointment,
    "consultar_mis_citas": Empty,
    "cancelar_cita": CancelAppointment,
    "ver_menu": Empty,
    "agregar_al_pedido": AddItem,
    "quitar_del_pedido": RemoveItem,
    "ver_pedido": Empty,
    "preparar_pedido": PlaceOrder,
    "confirmar_pedido": PlaceOrder,
    "cancelar_pedido": Empty,
    "pasar_a_humano": Handoff,
}


def notify(c, tenant_id, kind, key, payload, now):
    c.execute(
        """INSERT INTO outbox(tenant_id,type,event_key,payload_json,next_attempt_at,created_at)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT(tenant_id,event_key) DO NOTHING""",
        (tenant_id, kind, key, Jsonb(payload), now, now),
    )


@dataclass
class Context:
    c: object
    tenant_id: UUID
    customer_id: UUID
    conversation_id: UUID
    clock: object
    config: BusinessConfig
    operation_key: str
    confirmation: bool = False
    confirmation_received_at: datetime | None = None

    @property
    def now(self):
        return self.clock.now()

    def state(self):
        return self.c.execute(
            "SELECT state_json FROM conversations WHERE id=%s FOR UPDATE", (self.conversation_id,)
        ).fetchone()["state_json"]

    def save_state(self, state):
        self.c.execute(
            "UPDATE conversations SET state_json=%s,version=version+1 WHERE id=%s",
            (Jsonb(state), self.conversation_id),
        )


def audit(ctx, action, entity, entity_id):
    ctx.c.execute(
        "INSERT INTO audit_events(tenant_id,actor,action,entity,entity_id,created_at) VALUES(%s,'tool',%s,%s,%s,%s)",
        (ctx.tenant_id, action, entity, entity_id, ctx.now),
    )


def propose(ctx, kind, args, summary, version=None):
    state = ctx.state()
    state["pending"] = {
        "kind": kind,
        "args": args,
        "version": version,
        "expires_at": (ctx.now + timedelta(minutes=10)).isoformat(),
    }
    ctx.save_state(state)
    return {
        "status": "needs_confirmation",
        "summary": summary,
        "prompt": "¿Confirmas? Escribe confirmar o no.",
    }


def consume_confirmation(ctx, kind, args, version=None):
    from datetime import datetime

    pending = ctx.state().get("pending")
    if not ctx.confirmation or not pending or pending["kind"] != kind or pending["args"] != args:
        raise BusinessError("confirmation_required", "Primero revisa el resumen y confirma")
    if datetime.fromisoformat(pending["expires_at"]) < ctx.now or pending.get("version") != version:
        raise BusinessError("confirmation_expired", "El resumen cambió o expiró; revisa y confirma de nuevo")
    if ctx.confirmation_received_at is not None:
        summary = ctx.c.execute(
            "SELECT delivered_at FROM outbound WHERE event_key=%s AND conversation_id=%s AND status='sent'",
            (pending.get("summary_event_key"), ctx.conversation_id),
        ).fetchone()
        if (
            not summary
            or not summary["delivered_at"]
            or ctx.confirmation_received_at < summary["delivered_at"]
        ):
            raise BusinessError("summary_not_delivered", "Espera a recibir el resumen antes de confirmar")
    state = ctx.state()
    state.pop("pending", None)
    ctx.save_state(state)


def service(ctx, code):
    if not ctx.config.tenant.features.appointments:
        raise BusinessError("disabled", "Este negocio no ofrece citas")
    result = ctx.c.execute("SELECT * FROM services WHERE code=%s AND active", (code,)).fetchone()
    if not result:
        raise BusinessError("unknown_service", "Servicio inexistente")
    return result


def appointment(ctx, args, create=False):
    item = service(ctx, args["service_code"])
    quote_version = {key: item[key] for key in ("minutes", "buffer_minutes", "price_cents")}
    start = local_instant(args["starts_at_local"], ZoneInfo(ctx.config.tenant.timezone))
    day = start.astimezone(ZoneInfo(ctx.config.tenant.timezone)).date()
    valid = dict(candidates(ctx.config, item, day, day, ctx.now))
    if start not in valid:
        raise BusinessError("invalid_slot", "Horario fuera de disponibilidad, anticipación o plazo")
    if not create:
        busy = ctx.c.execute(
            "SELECT 1 FROM appointments WHERE status='confirmed' AND starts_at<%s AND ends_at>%s",
            (valid[start], start),
        ).fetchone()
        if busy:
            raise BusinessError("slot_taken", "Ese horario ya está ocupado")
        return propose(
            ctx,
            "crear_cita",
            args,
            f"{item['name']} · {start.astimezone(ZoneInfo(ctx.config.tenant.timezone)).isoformat()} · ${item['price_cents'] / 100:.2f}",
            quote_version,
        )
    consume_confirmation(ctx, "crear_cita", args, quote_version)
    resource = ctx.c.execute("SELECT id FROM resources").fetchone()["id"]
    try:
        with ctx.c.transaction():
            row = ctx.c.execute(
                """INSERT INTO appointments
                (tenant_id,resource_id,customer_id,service_id,starts_at,service_ends_at,ends_at,operation_key,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    ctx.tenant_id,
                    resource,
                    ctx.customer_id,
                    item["id"],
                    start,
                    start + timedelta(minutes=item["minutes"]),
                    valid[start],
                    ctx.operation_key,
                    ctx.now,
                ),
            ).fetchone()
    except ExclusionViolation:
        return {
            "status": "slot_taken",
            "alternatives": [s.isoformat() for s in available(ctx.c, ctx.config, item, day, day, ctx.now)],
        }
    ctx.c.execute("UPDATE customers SET name=%s WHERE id=%s", (args["customer_name"], ctx.customer_id))
    notify(
        ctx.c,
        ctx.tenant_id,
        "appointment_created",
        f"appointment:{row['id']}",
        {"appointment_id": str(row["id"]), "conversation_id": str(ctx.conversation_id)},
        ctx.now,
    )
    audit(ctx, "create", "appointment", row["id"])
    return {"status": "confirmed", "appointment_id": str(row["id"]), "starts_at": start.isoformat()}


def cart(ctx, create=False):
    if not ctx.config.tenant.features.orders:
        raise BusinessError("disabled", "Este negocio no acepta pedidos")
    row = ctx.c.execute(
        "SELECT * FROM orders WHERE customer_id=%s AND status='draft' FOR UPDATE", (ctx.customer_id,)
    ).fetchone()
    if not row and create:
        row = ctx.c.execute(
            "INSERT INTO orders(tenant_id,customer_id,created_at) VALUES(%s,%s,%s) RETURNING *",
            (ctx.tenant_id, ctx.customer_id, ctx.now),
        ).fetchone()
    return row


def cart_view(ctx, row):
    if not row:
        return {"status": "empty", "items": [], "total_cents": 0}
    items = ctx.c.execute(
        """SELECT m.code,m.name,i.qty,i.unit_price_cents FROM order_items i
        JOIN menu_items m ON m.id=i.menu_item_id WHERE order_id=%s ORDER BY m.code""",
        (row["id"],),
    ).fetchall()
    subtotal = sum(i["qty"] * i["unit_price_cents"] for i in items)
    return {
        "order_id": str(row["id"]),
        "status": row["status"],
        "items": items,
        "subtotal_cents": subtotal,
        "total_cents": subtotal + row["delivery_fee_cents"],
    }


def place_order(ctx, args, commit=False):
    row = cart(ctx)
    if not row:
        raise BusinessError("empty_cart", "Agrega productos antes de confirmar")
    rules = ctx.config.orders
    local = ctx.now.astimezone(ZoneInfo(ctx.config.tenant.timezone))
    if not any(a <= local.strftime("%H:%M") < b for a, b in rules.hours.get(DAYS[local.weekday()], [])):
        raise BusinessError("closed", "El negocio está cerrado para pedidos")
    if args["fulfillment"] not in rules.accepts:
        raise BusinessError("fulfillment_unavailable", "Este tipo de entrega no está disponible")
    if args["fulfillment"] == "delivery" and not args["address_note"].strip():
        raise BusinessError("address_required", "Indica una dirección de entrega")
    view = cart_view(ctx, row)
    if not view["items"] or view["subtotal_cents"] < cents(rules.min_order_mxn):
        raise BusinessError("minimum_order", f"El mínimo es ${rules.min_order_mxn}")
    invalid = ctx.c.execute(
        """SELECT 1 FROM order_items i JOIN menu_items m ON m.id=i.menu_item_id
        WHERE i.order_id=%s AND (NOT m.available OR m.price_cents<>i.unit_price_cents)""",
        (row["id"],),
    ).fetchone()
    if invalid:
        raise BusinessError("catalog_changed", "El catálogo cambió; quita y vuelve a agregar los productos")
    fee = cents(rules.delivery_fee_mxn) if args["fulfillment"] == "delivery" else 0
    total = view["subtotal_cents"] + fee
    if not commit:
        lines = ", ".join(f"{i['qty']} × {i['name']}" for i in view["items"])
        return propose(
            ctx,
            "confirmar_pedido",
            args,
            f"{lines} · {'recolección' if args['fulfillment'] == 'pickup' else 'entrega en ' + args['address_note']} · envío ${fee / 100:.2f} · total ${total / 100:.2f}",
            row["version"],
        )
    consume_confirmation(ctx, "confirmar_pedido", args, row["version"])
    ctx.c.execute(
        """UPDATE orders SET status='placed',fulfillment=%s,address_note=%s,total_cents=%s,
        delivery_fee_cents=%s,version=version+1 WHERE id=%s""",
        (args["fulfillment"], args["address_note"], total, fee, row["id"]),
    )
    ctx.c.execute("UPDATE customers SET name=%s WHERE id=%s", (args["customer_name"], ctx.customer_id))
    notify(
        ctx.c,
        ctx.tenant_id,
        "order_placed",
        f"order:{row['id']}",
        {"order_id": str(row["id"]), "conversation_id": str(ctx.conversation_id)},
        ctx.now,
    )
    audit(ctx, "place", "order", row["id"])
    return {"status": "placed", "order_id": str(row["id"]), "total_cents": total}


def handoff(ctx, reason, extend=True):
    if extend:
        ctx.c.execute(
            "UPDATE conversations SET mode='human',human_until=%s WHERE id=%s",
            (ctx.now + timedelta(hours=ctx.config.tenant.handoff_hours), ctx.conversation_id),
        )
        audit(ctx, "handoff", "conversation", ctx.conversation_id)
    row = ctx.c.execute(
        "SELECT last_handoff_notice_at FROM conversations WHERE id=%s", (ctx.conversation_id,)
    ).fetchone()
    if not row["last_handoff_notice_at"] or row["last_handoff_notice_at"] <= ctx.now - timedelta(minutes=10):
        recent = ctx.c.execute(
            "SELECT text FROM messages WHERE conversation_id=%s ORDER BY received_seq DESC LIMIT 3",
            (ctx.conversation_id,),
        ).fetchall()
        notify(
            ctx.c,
            ctx.tenant_id,
            "handoff",
            f"handoff:{ctx.conversation_id}:{ctx.operation_key}",
            {
                "conversation_id": str(ctx.conversation_id),
                "reason": reason,
                "recent_messages": [r["text"] for r in reversed(recent)],
            },
            ctx.now,
        )
        ctx.c.execute(
            "UPDATE conversations SET last_handoff_notice_at=%s WHERE id=%s", (ctx.now, ctx.conversation_id)
        )
    return {"status": "human", "message": "Te paso con una persona."}


def _execute(ctx, name, args):
    if name == "consultar_disponibilidad":
        if args["date_to"] < args["date_from"]:
            raise BusinessError("invalid_range", "Rango de fechas invertido")
        slots = available(
            ctx.c, ctx.config, service(ctx, args["service_code"]), args["date_from"], args["date_to"], ctx.now
        )
        return {"slots": [s.astimezone(ZoneInfo(ctx.config.tenant.timezone)).isoformat() for s in slots]}
    if name in {"preparar_cita", "crear_cita"}:
        return appointment(ctx, args, create=name == "crear_cita")
    if name == "consultar_mis_citas":
        rows = ctx.c.execute(
            "SELECT id,starts_at,status FROM appointments WHERE customer_id=%s AND status='confirmed' ORDER BY starts_at",
            (ctx.customer_id,),
        ).fetchall()
        return {
            "appointments": [
                {"id": str(r["id"]), "starts_at": r["starts_at"].isoformat(), "status": r["status"]}
                for r in rows
            ]
        }
    if name == "cancelar_cita":
        row = ctx.c.execute(
            "SELECT * FROM appointments WHERE id=%s AND customer_id=%s FOR UPDATE",
            (args["appointment_id"], ctx.customer_id),
        ).fetchone()
        if not row:
            raise BusinessError("not_found", "No se encontró una cita propia")
        if row["status"] != "confirmed":
            return {"status": row["status"]}
        if row["starts_at"] < ctx.now + timedelta(minutes=ctx.config.policies.cancellation_notice_minutes):
            raise BusinessError("cancellation_window", "La cita está fuera del plazo de cancelación")
        json_args = {"appointment_id": str(args["appointment_id"])}
        if not ctx.confirmation:
            return propose(ctx, name, json_args, "Cancelar tu cita del " + row["starts_at"].isoformat())
        consume_confirmation(ctx, name, json_args)
        ctx.c.execute("UPDATE appointments SET status='cancelled' WHERE id=%s", (row["id"],))
        notify(
            ctx.c,
            ctx.tenant_id,
            "appointment_cancelled",
            f"cancel:{row['id']}",
            {"appointment_id": str(row["id"])},
            ctx.now,
        )
        audit(ctx, "cancel", "appointment", row["id"])
        return {"status": "cancelled"}
    if name == "ver_menu":
        return {
            "items": ctx.c.execute(
                "SELECT code,name,price_cents FROM menu_items WHERE available ORDER BY code"
            ).fetchall()
        }
    if name in {"agregar_al_pedido", "quitar_del_pedido"}:
        row = cart(ctx, create=name == "agregar_al_pedido")
        if not row:
            raise BusinessError("empty_cart", "No hay pedido abierto")
        item = ctx.c.execute("SELECT * FROM menu_items WHERE code=%s", (args["item_code"],)).fetchone()
        if not item or (name == "agregar_al_pedido" and not item["available"]):
            raise BusinessError("item_unavailable", "Producto no disponible")
        existing = ctx.c.execute(
            "SELECT * FROM order_items WHERE order_id=%s AND menu_item_id=%s", (row["id"], item["id"])
        ).fetchone()
        if name == "agregar_al_pedido":
            qty = (existing["qty"] if existing else 0) + args["qty"]
            if qty > 99:
                raise BusinessError("quantity_limit", "Máximo 99 unidades por producto")
            ctx.c.execute(
                """INSERT INTO order_items(tenant_id,order_id,menu_item_id,qty,unit_price_cents,note)
                VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(order_id,menu_item_id) DO UPDATE SET qty=excluded.qty""",
                (ctx.tenant_id, row["id"], item["id"], qty, item["price_cents"], args["note"]),
            )
        elif existing:
            qty = existing["qty"] - (args["qty"] or existing["qty"])
            if qty <= 0:
                ctx.c.execute("DELETE FROM order_items WHERE id=%s", (existing["id"],))
            else:
                ctx.c.execute("UPDATE order_items SET qty=%s WHERE id=%s", (qty, existing["id"]))
        ctx.c.execute("UPDATE orders SET version=version+1 WHERE id=%s", (row["id"],))
        state = ctx.state()
        state.pop("pending", None)
        ctx.save_state(state)
        return cart_view(ctx, row)
    if name == "ver_pedido":
        row = ctx.c.execute(
            "SELECT * FROM orders WHERE customer_id=%s ORDER BY created_at DESC LIMIT 1", (ctx.customer_id,)
        ).fetchone()
        return cart_view(ctx, row)
    if name in {"preparar_pedido", "confirmar_pedido"}:
        return place_order(ctx, args, commit=name == "confirmar_pedido")
    if name == "cancelar_pedido":
        row = ctx.c.execute(
            "SELECT * FROM orders WHERE customer_id=%s ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
            (ctx.customer_id,),
        ).fetchone()
        if not row or row["status"] not in {"draft", "placed"}:
            raise BusinessError("invalid_transition", "Solo puedes cancelar pedidos borrador o pendientes")
        if not ctx.confirmation:
            return propose(ctx, name, {}, "Cancelar tu pedido", row["version"])
        consume_confirmation(ctx, name, {}, row["version"])
        ctx.c.execute("UPDATE orders SET status='cancelled',version=version+1 WHERE id=%s", (row["id"],))
        notify(
            ctx.c,
            ctx.tenant_id,
            "order_cancelled",
            f"cancel-order:{row['id']}",
            {"order_id": str(row["id"])},
            ctx.now,
        )
        audit(ctx, "cancel", "order", row["id"])
        return {"status": "cancelled"}
    if name == "pasar_a_humano":
        return handoff(ctx, args["reason"])
    raise BusinessError("unknown_tool", "Herramienta inexistente")


def execute(ctx, name, arguments):
    if name not in SCHEMAS:
        raise BusinessError("unknown_tool", "Herramienta inexistente")
    parsed = SCHEMAS[name].model_validate(arguments)
    fingerprint = hashlib.sha256(
        json.dumps({"name": name, "args": parsed.model_dump(mode="json")}, sort_keys=True).encode()
    ).hexdigest()
    key = f"{ctx.operation_key}:{fingerprint}"
    cached = ctx.c.execute(
        "SELECT result_json FROM tool_results WHERE conversation_id=%s AND operation_key=%s",
        (ctx.conversation_id, key),
    ).fetchone()
    if cached:
        return cached["result_json"]
    result = _execute(ctx, name, parsed.model_dump())
    ctx.c.execute(
        "INSERT INTO tool_results(tenant_id,conversation_id,operation_key,result_json) VALUES(%s,%s,%s,%s)",
        (ctx.tenant_id, ctx.conversation_id, key, Jsonb(result)),
    )
    return result
