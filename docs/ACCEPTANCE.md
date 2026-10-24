# Evidencia de aceptación local

Revisión: 24 de octubre de 2026. Alcance: entrega local E1–E8 del documento, con adaptadores Fake y PostgreSQL real. No se conectaron proveedores de mensajería, LLM, correo ni almacenamiento externo.

## Resultados

- `uv run pytest -q --tb=short`: **67 aprobadas**, cero fallos, 225.87 segundos. Incluye 30 conversaciones con aserciones sobre el estado final en BD (30/30), sin omitir pruebas de integración.
- `uv run ruff check app tests`: correcto; `uv run ruff format --check app tests`: 42 archivos conformes.
- `uv run pip-audit`: ninguna vulnerabilidad conocida en dependencias. El paquete local `barrio-bot` no está publicado en PyPI y no es auditable como paquete externo.
- La suite emite un aviso de deprecación de Starlette al usar httpx con TestClient; no afecta los resultados.
- API y worker nativos funcionando con PostgreSQL 16.15, sin variables secretas. Se probaron los scripts de arranque y parada.
- Dockerfile construido con Python 3.12; Compose levanta `app`, `worker` y `postgres:16`. `seed demo` y `/healthz` funcionan dentro del contenedor. UID de aplicación: 10001, no root.
- `sim run --file examples/order.yaml` dentro del contenedor confirmó un pedido de $90 y entregó su aviso. Se creó un backup cifrado; la agenda devolvió HTTP 200. Los hashes del orquestador, worker y enriquecimiento de avisos coinciden con los archivos locales probados.
- Contra la API nativa en ejecución se probaron Aceptar, Rechazar, ETA de 30 minutos, Tomar chat y Devolver al bot. Se comprobaron las transiciones en PostgreSQL y el HTML final de la bandeja.

## Requisitos e invariantes

| Requisito | Evidencia |
|---|---|
| R1–R2: sin conexiones reales, puertos | Fake persiste en BD; pruebas unitarias de contratos y adaptadores reales `NotConfigured` |
| R3–R4: local, PostgreSQL, sin secretos | Compose de tres servicios, migraciones y seed; suite con bases PostgreSQL temporales |
| R5: guardia producción | Pruebas de Settings y rutas dev ausentes fuera de dev |
| R6: flujo local | Simulador, avisos, acciones del dueño y agenda alimentados por la misma BD y tools |
| I1: mensaje único | Cinco entregas del mismo evento generan un mensaje y una respuesta |
| I2: sin solapamientos | Veinte escritores concurrentes producen exactamente una cita; exclusión GiST |
| I3–I4: precios e identidad | Catálogo servidor, esquemas estrictos y prueba con LLM malicioso que intenta pasar precio e identidad |
| I5: aislamiento | Rol real sin BYPASSRLS, lecturas/escrituras cruzadas rechazadas, FK compuestas y contexto reiniciado |
| I6: webhook rápido | Respuesta menor de 500 ms sin ejecutar el LLM |
| I7: degradación | Timeout, respuesta inválida y presupuesto agotado: respuesta determinista y handoff |
| I8: aviso atómico | Cita y outbox en una transacción; fallo de outbox revierte la reserva y el reintento crea un solo aviso |

## Hitos

| Hito | Entrega y validación |
|---|---|
| E1 | Puertos, Fake/Real, migraciones, roles, RLS, configuración y guardia producción |
| E2 | Ingesta, idempotencia, debounce, cola, recuperación de lease y workers concurrentes |
| E3 | Disponibilidad, horarios, excepciones, duración/buffer, DST y exclusión concurrente |
| E4 | Carritos, precios servidor, confirmación, mínimos, entrega, cancelación y transiciones del dueño |
| E5 | ScriptedLLM, contexto limitado, reservas de presupuesto, degradación y 30 evals |
| E6 | Outbox con reintentos, avisos enriquecidos, resumen diario, botones y handoff manual |
| E7 | Seed de tres negocios, CLI de simulación, bandejas dev y agenda privada con revocación |
| E8 | Adaptadores desconectados, CONNECTING, RUNBOOK, SECURITY, métricas SQL y workflow CI |

## Casos adicionales

- Confirmar antes de la entrega del resumen no crea una reserva. Cambios de carrito o cotización invalidan la confirmación.
- BAJA durante una llamada LLM cancela la respuesta y deja solo el acuse de baja.
- Los mensajes del cliente no prolongan la atención humana; `menú` devuelve el control al bot.
- Intentar cancelar la cita de otro cliente no modifica la BD.
- Agenda escapa HTML, omite notas libres y aplica CSP, no-store, noindex y no-referrer.
- Retención elimina mensajes y copias derivadas. Borrado por contacto conserva los otros clientes.
- Backup cifrado restaurado en otra base real; se verifican datos, permisos y RLS. Restaurar sobre una base ocupada se rechaza.
- Dependencias actualizadas a cryptography 50.0.2 tras detectar vulnerabilidades en la versión inicial.

## Entornos y límites de la evidencia

La suite se ejecutó con Python 3.13 en Windows y PostgreSQL 16. La imagen usa Python 3.12; el workflow de CI replica pruebas con 3.12/PostgreSQL 16. El workflow está incluido, pero no se ejecutó en GitHub porque el proyecto no se publicó.

Docker se verificó con un daemon aislado en WSL y puertos 18000/15432, para coexistir con el entorno nativo 8000/5432. Se construyó la imagen con `docker build --network host` y se levantó con `docker compose up -d --no-build`. Se corrigió la resolución DNS del entorno instalando su requisito iptables y reiniciando ese daemon. No se necesitó modificar el código de conexión a BD.

Se revisó visualmente el simulador y se creó un pedido desde el navegador. La revisión final del navegador integrado no pudo adjuntar su pestaña; las acciones y páginas también tienen pruebas HTTP y de BD. No se afirma una segunda revisión visual completa después de ese fallo de la herramienta.

Las evals Fake verifican la tubería, no calidad conversacional de un LLM real, formatos de WaSender ni eficacia contra bloqueos de WhatsApp. El piloto comercial, conexiones reales y despliegue público pertenecen a la siguiente etapa definida en la arquitectura.
