# Seguridad de esta entrega

| Control | Implementación / verificación |
|---|---|
| Aislamiento por negocio | RLS habilitado y forzado; rol ws_app sin privilegios elevados; FK compuestas y tests de lectura/escritura cruzada |
| Identidad confiable | Token de cuenta resuelto en servidor; tools no aceptan tenant_id/customer_id; esquema extra=forbid |
| Citas solapadas | Exclusión GiST con buffer, prueba de 20 escritores concurrentes |
| Precios y confirmación | Catálogo en BD, centavos, rangos de cantidad; confirmación vinculada al resumen y versión |
| Reintentos | Claves durables de tool/outbox/outbound; SKIP LOCKED, lease y advisory lock de sesión |
| Webhook local | Token de 32 bytes con hash, antigüedad máxima, cuerpo máximo 16 KiB, límites por IP y cola |
| Firmas de proveedor | Pendientes en adaptadores reales; estos lanzan NotConfigured, no aceptan payloads inventados |
| Salida y presupuesto | 20/contacto/hora, 150/cuenta/hora, 800/cuenta/día; reserva atómica de tokens y degradación |
| Agenda privada | Token revocable con hash, escape HTML, CSP, no-store, noindex, no-referrer; sin notas libres |
| Acciones dev | Solo APP_ENV=dev, token anti-CSRF y verificación de origen; Compose solo loopback |
| Secretos | Fernet, claves locales generadas y excluidas de Git; entorno para futuros conectores |
| Datos sensibles | Aviso local, filtro conservador antes de persistir, handoff; limitación explícita para texto libre |
| Retención | Purga de mensajes y copias derivadas; borrado por contacto con CLI |
| Logs | IDs y tipo de excepción, sin cuerpos ni tokens; access log HTTP desactivado |
| Dependencias | uv.lock, Ruff, pytest, pip-audit y workflow CI |
| Contenedor | Usuario no root y tres servicios locales |
| Backup | pg_dump cifrado, retención 14 días, restauración en base distinta; prueba documentada en ACCEPTANCE |

La autenticación `trust` de PostgreSQL y las rutas administrativas dev no son aptas para exposición pública. Las reglas de producción rechazan todos los adaptadores fake/dev/local. El piloto requiere configurar el responsable del aviso de privacidad, proveedor real y garantías de tratamiento de datos, autenticación de webhooks, TLS y secretos; se detalla en CONNECTING.

No se afirma resistencia general contra prompt injection ni detección perfecta de información sensible. Las operaciones de negocio se protegen mediante validación y contexto de servidor, aunque el modelo produzca una llamada maliciosa.
