import json
import secrets
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from app.admin import admin_connection, demo_tokens, tenant_by_slug
from app.core.ingest import ingest, token_hash
from app.errors import BusinessError, NotConfigured
from app.notify.actions import owner_action
from app.runtime import Runtime
from app.settings import Settings

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "web"))
templates.env.filters["money"] = lambda value: f"${value / 100:,.2f}"


def create_app(runtime=None, settings=None):
    settings = settings or (runtime.settings if runtime else Settings())

    @asynccontextmanager
    async def lifespan(app):
        app.state.rt = runtime or Runtime(settings)
        app.state.csrf = secrets.token_urlsafe(32)
        yield
        if not runtime:
            app.state.rt.close()

    api = FastAPI(
        title="Barrio Bot · entorno local",
        lifespan=lifespan,
        docs_url="/dev/docs" if settings.dev_routes else None,
        redoc_url=None,
        openapi_url="/dev/openapi.json" if settings.dev_routes else None,
    )
    api.mount("/static", StaticFiles(directory=str(ROOT / "web/static")), name="static")
    rates = OrderedDict()

    @api.middleware("http")
    async def guard(request, call_next):
        now = datetime.now().timestamp()
        ip = request.client.host if request.client else "unknown"
        key = (ip, request.url.path.split("/")[1])
        start, count = rates.get(key, (now, 0))
        if now - start >= 60:
            start, count = now, 0
        rates[key] = (start, count + 1)
        rates.move_to_end(key)
        while len(rates) > 10000:
            rates.popitem(last=False)
        if count >= 240:
            return JSONResponse({"detail": "Demasiadas solicitudes"}, 429, headers={"Retry-After": "60"})
        if request.method == "POST":
            chunks = []
            size = 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > 16384:
                    return JSONResponse({"detail": "Cuerpo demasiado grande"}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            if request.url.path.startswith("/dev/"):
                given = request.headers.get("x-dev-csrf", "")
                if not secrets.compare_digest(given, request.app.state.csrf):
                    return JSONResponse({"detail": "Token local inválido"}, 403)
                origin = request.headers.get("origin")
                if origin and origin != str(request.base_url).rstrip("/"):
                    return JSONResponse({"detail": "Origen no permitido"}, 403)
        response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Robots-Tag": "noindex, nofollow",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "Content-Security-Policy": "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }
        )
        return response

    @api.exception_handler(BusinessError)
    async def business_error(request, error):
        status = {"not_found": 404, "queue_full": 503, "body_too_large": 413}.get(error.code, 422)
        return JSONResponse({"detail": str(error), "code": error.code}, status)

    @api.exception_handler(NotConfigured)
    async def not_configured(request, error):
        return JSONResponse({"detail": str(error)}, 503)

    @api.get("/healthz")
    @api.get("/readyz")
    def health(request: Request):
        try:
            with request.app.state.rt.db.pool.connection(timeout=2) as c:
                c.execute("SELECT 1")
            return {"status": "ok", "database": "postgresql"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    @api.get("/", include_in_schema=False)
    def home():
        if settings.dev_routes:
            return RedirectResponse("/dev/inbox")
        raise HTTPException(404)

    @api.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return templates.TemplateResponse(request=request, name="privacy.html", context={})

    @api.post("/webhooks/{provider}/{token}")
    async def webhook(provider: str, token: str, request: Request):
        from starlette.concurrency import run_in_threadpool

        try:
            return await run_in_threadpool(
                ingest, request.app.state.rt, provider, token, await request.body(), dict(request.headers)
            )
        except ValidationError:
            raise HTTPException(422, "Evento inválido") from None

    @api.get("/a/{token}", response_class=HTMLResponse)
    def agenda(token: str, request: Request):
        rt = request.app.state.rt
        owner = rt.db.resolve("agenda", token_hash(token))
        if not owner:
            raise HTTPException(404, "Enlace inválido o revocado")
        with rt.db.tx(owner["tenant_id"]) as c:
            tenant = c.execute("SELECT * FROM tenants WHERE id=%s", (owner["tenant_id"],)).fetchone()
            zone = ZoneInfo(tenant["timezone"])
            today = rt.clock.now().astimezone(zone).date()
            beginning = datetime.combine(today, time(), tzinfo=zone)
            end = datetime.combine(today + timedelta(days=8), time(), tzinfo=zone)
            appointments = c.execute(
                """SELECT a.id,a.starts_at,a.service_ends_at,a.status,s.name service,
                u.name customer,u.phone_e164 FROM appointments a JOIN services s ON s.id=a.service_id
                JOIN customers u ON u.id=a.customer_id WHERE a.starts_at>=%s AND a.starts_at<%s ORDER BY a.starts_at""",
                (beginning, end),
            ).fetchall()
            orders = c.execute("""SELECT o.id,o.status,o.fulfillment,o.total_cents,o.owner_eta_minutes,u.name customer,u.phone_e164
                FROM orders o JOIN customers u ON u.id=o.customer_id WHERE o.status IN ('placed','accepted') ORDER BY o.created_at""").fetchall()
        for row in appointments:
            row["local_start"] = row["starts_at"].astimezone(zone)
            row["local_end"] = row["service_ends_at"].astimezone(zone)
        return templates.TemplateResponse(
            request=request,
            name="agenda.html",
            context={
                "tenant": tenant,
                "appointments": appointments,
                "orders": orders,
                "today": today,
                "zone": tenant["timezone"],
            },
        )

    if settings.dev_routes:

        def dev_context(request, slug=None):
            rt = request.app.state.rt
            with admin_connection(settings) as c:
                tenants = c.execute(
                    "SELECT id,slug,name,timezone,config_json FROM tenants ORDER BY slug"
                ).fetchall()
            selected = next((t for t in tenants if t["slug"] == slug), tenants[0] if tenants else None)
            tokens = demo_tokens(rt)
            example = None
            if selected and selected["config_json"]["tenant"]["features"]["appointments"]:
                from app.config import BusinessConfig
                from app.core.scheduling import available

                config = BusinessConfig.model_validate(selected["config_json"])
                local = rt.clock.now().astimezone(ZoneInfo(selected["timezone"]))
                with rt.db.tx(selected["id"]) as c:
                    service = c.execute(
                        "SELECT * FROM services WHERE active ORDER BY code LIMIT 1"
                    ).fetchone()
                    if service:
                        slots = available(
                            c, config, service, local.date(), local.date() + timedelta(days=7), rt.clock.now()
                        )
                        if slots:
                            slot = slots[0].astimezone(ZoneInfo(selected["timezone"]))
                            example = {
                                "code": service["code"],
                                "date": slot.strftime("%Y-%m-%d"),
                                "time": slot.strftime("%Y-%m-%dT%H:%M"),
                            }
            return {
                "tenants": tenants,
                "selected": selected,
                "csrf": request.app.state.csrf,
                "tokens": tokens,
                "example": example,
            }

        @api.get("/dev/inbox", response_class=HTMLResponse)
        def inbox(request: Request, tenant: str | None = None):
            return templates.TemplateResponse(
                request=request, name="inbox.html", context=dev_context(request, tenant)
            )

        @api.get("/dev/messages")
        def messages(request: Request, tenant: str, contact: str = "cliente-demo"):
            rt = request.app.state.rt
            row = tenant_by_slug(settings, tenant)
            with rt.db.tx(row["id"]) as c:
                messages = c.execute(
                    """SELECT m.id,m.direction,m.text,m.created_at FROM messages m
                    JOIN conversations v ON v.id=m.conversation_id JOIN customers u ON u.id=v.customer_id
                    WHERE u.contact_id=%s ORDER BY m.received_seq DESC LIMIT 100""",
                    (contact,),
                ).fetchall()
                state = c.execute(
                    "SELECT v.mode,u.opted_out FROM conversations v JOIN customers u ON u.id=v.customer_id WHERE u.contact_id=%s",
                    (contact,),
                ).fetchone()
            return {"messages": list(reversed(messages)), "state": state}

        @api.post("/dev/chat")
        async def chat(request: Request):
            body = await request.json()
            rt = request.app.state.rt
            slug = body.get("tenant", "")
            token = demo_tokens(rt).get(slug, {}).get("webhook_token")
            if not token:
                raise HTTPException(404, "Ejecuta seed demo para crear este negocio")
            payload = {
                "provider_message_id": str(uuid4()),
                "contact_id": body.get("contact", "cliente-demo"),
                "text": body.get("text", ""),
                "timestamp": rt.clock.now().isoformat(),
                "is_from_me": body.get("is_from_me", False),
                "kind": body.get("kind", "text"),
            }
            from starlette.concurrency import run_in_threadpool

            try:
                return await run_in_threadpool(ingest, rt, "fake", token, json.dumps(payload).encode())
            except ValidationError:
                raise HTTPException(422, "Mensaje inválido") from None

        @api.get("/dev/notifications", response_class=HTMLResponse)
        def notifications(request: Request, tenant: str | None = None):
            ctx = dev_context(request, tenant)
            rows = []
            if ctx["selected"]:
                with request.app.state.rt.db.tx(ctx["selected"]["id"]) as c:
                    rows = c.execute(
                        "SELECT * FROM fake_notifications ORDER BY created_at DESC LIMIT 100"
                    ).fetchall()
                    owner = c.execute("SELECT id FROM owners").fetchone()
                    ctx["owner_id"] = owner["id"]
                    for row in rows:
                        zone = ZoneInfo(ctx["selected"]["timezone"])
                        row["created_at"] = row["created_at"].astimezone(zone)
                        payload = row["payload_json"]
                        if payload.get("order_id"):
                            row["order"] = c.execute(
                                "SELECT * FROM orders WHERE id=%s", (UUID(payload["order_id"]),)
                            ).fetchone()
                            row["items"] = c.execute(
                                "SELECT m.name,i.qty,i.unit_price_cents FROM order_items i JOIN menu_items m ON m.id=i.menu_item_id WHERE order_id=%s",
                                (UUID(payload["order_id"]),),
                            ).fetchall()
                        if payload.get("appointment_id"):
                            row["appointment"] = c.execute(
                                "SELECT a.starts_at,s.name,c.name customer FROM appointments a JOIN services s ON s.id=a.service_id JOIN customers c ON c.id=a.customer_id WHERE a.id=%s",
                                (UUID(payload["appointment_id"]),),
                            ).fetchone()
                            if row["appointment"]:
                                row["appointment"]["starts_at"] = row["appointment"]["starts_at"].astimezone(
                                    zone
                                )
            ctx["notifications"] = rows
            return templates.TemplateResponse(request=request, name="notifications.html", context=ctx)

        @api.post("/dev/actions")
        async def action(request: Request):
            body = await request.json()
            tenant = tenant_by_slug(settings, body.get("tenant", ""))
            with request.app.state.rt.db.tx(tenant["id"]) as c:
                owner = c.execute("SELECT id FROM owners").fetchone()
            try:
                return owner_action(
                    request.app.state.rt,
                    tenant["id"],
                    owner["id"],
                    body["action"],
                    body["entity_id"],
                    body.get("eta"),
                )
            except (ValueError, KeyError) as error:
                if isinstance(error, BusinessError):
                    raise
                raise HTTPException(422, "Acción inválida") from None

    return api
