# Barrio Bot

MVP local para citas y pedidos de negocios pequeños. Implementa la entrega E1–E8 de [la arquitectura](docs/ARCHITECTURE.md): FastAPI, PostgreSQL real, worker, simulador, agenda y adaptadores Fake. Los conectores externos son esqueletos que fallan con `NotConfigured`.

## Ejecutar con Docker

```sh
docker compose up --build -d
docker compose exec app seed demo
```

Abre [el simulador](http://127.0.0.1:8000/dev/inbox) o [los avisos](http://127.0.0.1:8000/dev/notifications). La página del simulador contiene el enlace privado de agenda para cada negocio. `seed demo` también imprime esos enlaces. El worker debe estar en ejecución: las respuestas esperan el debounce y el retraso de escritura, hasta unos 11 segundos.

Compose publica exclusivamente en `127.0.0.1`. Su autenticación PostgreSQL `trust` es para desarrollo local, sin secretos requeridos. No publiques estos servicios ni reutilices este Compose en producción.

```sh
docker compose exec app sim chat --tenant salon-ejemplo --as cliente-uno
docker compose exec app sim run --file examples/order.yaml
```

## Ejecutar con Python y PostgreSQL

Requisitos: Python 3.12 o 3.13, `uv`, PostgreSQL 16 con `btree_gist`. Crea la base `barrio`, accesible como administrador mediante `DATABASE_URL_ADMIN`. Las migraciones crean el rol limitado `ws_app`; en entornos con contraseña debes configurarla por fuera del repositorio.

```sh
uv sync --frozen
uv run bot migrate
uv run seed demo
uv run bot api
# En otra terminal
uv run bot worker
```

En este equipo se instaló un PostgreSQL portátil dentro de `.runtime/postgresql`, con datos en `.runtime/pgdata`. Para levantar la base, API y worker en segundo plano desde PowerShell:

```powershell
./scripts/start-local.ps1
# Al terminar:
./scripts/stop-local.ps1 -Database
```

Estos scripts usan el PostgreSQL portátil ya instalado en este equipo; para otra máquina utiliza Docker o instala los requisitos anteriores. [RUNBOOK](docs/RUNBOOK.md) detalla operación, backups y borrado por cliente.

## Probar el flujo

En **Salón Jacaranda**, utiliza una fecha futura de lunes a sábado:

```text
horarios corte 2026-10-28
agendar corte 2026-10-28T12:00 Ana
confirmar
mis citas
```

En **Taquería La Esquina**:

```text
agregar taco_pastor 5
pedido delivery Calle Centro 10
confirmar
```

El total resulta $120: cinco tacos de $18 más $30 de envío. En avisos puedes aceptar, rechazar, indicar 15/30/45 minutos y tomar o devolver el chat. La agenda solo permite consultar.

Otros comandos del chat: `menú`, `ver pedido`, `quitar taco_pastor 1`, `cancelar pedido`, `cancelar cita <UUID>`, `humano`, `BAJA`. Las cancelaciones piden confirmación. `menú` devuelve el chat al bot. El simulador usa reglas explícitas, no un modelo de lenguaje real.

## Verificar

```sh
uv run pytest -q
uv run ruff check app tests
uv run ruff format --check app tests
uv run pip-audit
```

Los tests crean una base PostgreSQL temporal con nombre `barrio_test_<aleatorio>` y la eliminan al terminar. No usan SQLite. [Pruebas de invariantes](tests/test_integration.py), [guiones de conversación](tests/evals/conversations.yaml) y [resiliencia](tests/test_resilience.py).

## Estructura

| Ruta | Responsabilidad |
|---|---|
| `app/api.py`, `app/web/` | Webhooks, agenda, simulador, avisos y protección HTTP |
| `app/db/` | Migraciones, RLS y transacciones con contexto de negocio |
| `app/core/` | Disponibilidad, precios, confirmaciones, handoff y orquestación |
| `app/worker.py` | Cola, recuperación de leases, entregas, límites y scheduler |
| `app/ports/`, `app/channels/`, `app/llm/`, `app/notify/` | Contratos y adaptadores |
| `app/admin.py`, `app/cli.py`, `app/sim.py` | Administración local, semillas y guiones |
| `app/backup/` | Dumps cifrados y restauración en otra base |

Decisiones y mejoras: [DECISIONS](docs/DECISIONS.md). Conexiones futuras: [CONNECTING](docs/CONNECTING.md). Operación: [RUNBOOK](docs/RUNBOOK.md). Estado de aceptación: [ACCEPTANCE](docs/ACCEPTANCE.md).
