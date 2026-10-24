# Conectar después del MVP local

Los conectores reales fallan deliberadamente con `NotConfigured`. No introducir credenciales hasta que se implemente y pruebe el transporte. Los ejemplos son nombres de variables, no secretos.

| Pieza | Archivo | Configuración pendiente |
|---|---|---|
| WhatsApp de prueba | `app/channels/wasender/__init__.py` | Cuenta, token cifrado por canal, secreto de webhook, parser y fixtures reales |
| LLM | `app/llm/openai.py` | LLM_API_KEY, LLM_MODEL, transporte con timeout y política de no entrenamiento |
| Telegram | `app/notify/telegram.py` | TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET, TELEGRAM_ADMIN_CHAT_ID |
| Correo | `app/notify/email.py` | SMTP_URL o EMAIL_API_KEY, EMAIL_FROM y SPF/DKIM |
| Secretos | `app/adapters.py` | SECRETS_ADAPTER=env, MASTER_KEY Fernet |
| Backup remoto | `app/backup/s3.py` | BACKUP_TARGET_URL, credenciales S3, BACKUP_ENCRYPTION_KEY |

## WhatsApp

1. Usar un número de prueba propio. Consultar documentación oficial vigente antes de implementar cualquier endpoint o firma.
2. Capturar fixtures anonimizados de texto, grupo, multimedia, LID, mensaje manual, eco del bot y reentrega. Mantener todos los detalles de WaSender dentro de `channels/wasender` y resolver cada marca VERIFICAR-DOCS.
3. Implementar verificación de firma en tiempo constante sobre los bytes originales. Resolver la cuenta por token de ruta; jamás tomar tenant o cuenta del JSON.
4. Implementar envío, typing y estado. Resolver la idempotencia de envío y timeouts ambiguos del proveedor; no prometer exactly-once sin soporte verificable.
5. `bot channel add --tenant <slug> --provider wasender` crea la cuenta sin conectar; guardar las credenciales cifradas mediante SecretStore en `channel_accounts`.
6. Cambiar `CHANNEL_ADAPTER=wasender` solo al completar tests de contrato. Migrar al canal oficial creando otro adaptador sin cambiar las tools.

## LLM

Implementar `generate(messages, tools, max_tokens, temperature)`. Usar el timeout del cliente, no solo el del ejecutor; evitar reintentos infinitos y preservar el presupuesto compartido entre llamadas. Devolver uso real y costo según tarifas verificadas, no constantes supuestas. Ajustar prompts y añadir al menos 25 evals reales sobre estado final. No confundir las evals ScriptedLLM con calidad conversacional.

## Dueños

`bot owner link-code --tenant <slug>` genera un código de un solo uso, con 15 minutos de vigencia. Crear el deep link con el nombre del bot real. El webhook futuro verifica `secret_token`, vincula mediante `bind_owner` y traduce callbacks a `owner_action`; debe derivar owner/tenant desde el `chat_id` vinculado. Nunca usar el tenant que llegue en un botón sin verificarlo. La bandeja dev llama al mismo handler, pero su acceso local no reemplaza esa autenticación.

Telegram es principal y email es respaldo. Botones: aceptar, rechazar, ETA 15/30/45, tomar chat y devolver al bot. Activar FEATURE_OWNER_TRIGGERED_REPLIES únicamente si se desea enviar un estado dentro de una conversación reciente. OWNER_NOTIFY_WHATSAPP no está implementado ni habilitado en esta entrega; solo se reconsidera después de entrevistas del piloto.

## Hosting y operación

Crear `docker-compose.prod.yml` separado cuando exista hosting. Usar contraseñas de BD y roles separados, no publicar PostgreSQL, quitar montajes/dev/routes, cargar secretos fuera del repositorio y configurar PUBLIC_BASE_URL. Añadir proxy TLS (Caddy), dominio, límites del proxy y monitor externo a `/healthz`/`/readyz`. No activar APP_ENV=prod hasta retirar todos los adaptadores locales y verificar arranque.

Antes de piloto: aprobar aviso de privacidad del responsable, revisar retención/borrado, validar backup cifrado en S3 y restaurar en una base nueva, rotar tokens y claves, aplicar checklist de seguridad y pruebas de contrato. La eficacia de los límites anti-baneo y aceptación de Telegram solo se valida en vivo.
