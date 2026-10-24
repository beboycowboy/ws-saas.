# Operación local

## Arranque y parada

Con Docker: `docker compose up --build -d`, `docker compose exec app seed demo`. Ver logs con `docker compose logs -f app worker`. Detener con `docker compose down`; no usar `-v` salvo que se quiera borrar deliberadamente la base de demostración.

En este Windows, el PostgreSQL portátil queda en `.runtime/postgresql/pgsql/bin`. `./scripts/start-local.ps1` arranca base, API y worker en segundo plano. `./scripts/stop-local.ps1` detiene los procesos de la aplicación de este proyecto; agrega `-Database` para detener también su base portátil. Para operarlos manualmente:

```powershell
& .runtime/postgresql/pgsql/bin/pg_ctl.exe -D .runtime/pgdata -l .runtime/postgres.log -o '-h 127.0.0.1 -p 5432' -w start
uv run bot api
# Otra terminal:
uv run bot worker
# Detener PostgreSQL después de cerrar app/worker:
& .runtime/postgresql/pgsql/bin/pg_ctl.exe -D .runtime/pgdata -m fast -w stop
```

Los procesos lanzados para esta revisión están registrados en `.runtime/processes.json`; sus logs están en `.runtime/api.*.log` y `.runtime/worker.*.log`. Comprueba el proceso y su línea de comando antes de detener un PID guardado, porque Windows puede reutilizarlo.

## Diagnóstico

1. `/healthz` y `/readyz` deben responder 200.
2. Si el mensaje entra pero no hay respuesta, revisar que el worker está activo, el chat no está en human/BAJA y el mensaje no tiene más de 10 minutos.
3. La respuesta tarda hasta 2.5 s de debounce + 8 s de escritura. Las ráfagas prolongan debounce como máximo 10 s.
4. Jobs y outbox reintentan hasta 5 veces. `last_error` contiene solo el tipo de excepción; la bandeja registra una alerta al quedar `dead`.
5. La cola global se inspecciona por CLI/SQL administrador; las conexiones de app requieren contexto de tenant.

## CLI

```sh
bot tenant create --config examples/tenants/salon.yaml
bot tenant update --config negocio.yaml
bot tenant list
bot owner rotate-agenda-token --tenant salon-ejemplo
bot owner link-code --tenant salon-ejemplo
bot channel add --tenant salon-ejemplo --provider wasender
bot usage report --since 7d
bot conversation export --tenant salon-ejemplo --since 7d
bot purge --older-than 90d
bot customer erase --tenant salon-ejemplo --contact cliente-uno --confirm-contact cliente-uno
bot backup create
bot backup restore <archivo.dump.enc> --destination-url <URL-de-base-nueva>
```

Los enlaces privados se imprimen solo al generarlos; los logs HTTP de acceso se desactivan para no registrar tokens en URLs. Después de rotar agenda, el enlace anterior devuelve 404. Actualizar el enlace guardado del simulador si se usa la misma semilla.

## Pruebas de fallos

`FAKE_LLM_FAILURE=timeout|error|invalid` simula las tres clases de fallo. `FAKE_NOTIFIER_FAILURES=2` falla las primeras dos entregas del proceso. Reiniciar worker tras cambiar las variables. FakeClock se inyecta en tests para avanzar sin esperar. No cambiar el reloj de Windows para probar.

## Backups y secretos

El worker crea un backup diario, con exclusión entre workers y reintento tras fallo. El dump se cifra con Fernet antes de guardarse y se retiene 14 días en `.runtime/backups`. Usa `DATABASE_URL_BACKUP`, cuyo rol `ws_backup` tiene lectura y BYPASSRLS para incluir todos los negocios, sin permisos de escritura. El rol de aplicación sigue sujeto a RLS.

Desarrollo genera `.runtime/backup.key`; guarda esa clave aparte para poder restaurar. MASTER_KEY y BACKUP_ENCRYPTION_KEY no son intercambiables. El backup nunca incluye las claves como datos de negocio. La restauración exige una base diferente y falla si encuentra objetos existentes. La prueba automatizada restaura datos, privilegios y aislamiento en otra base real.

El borrado por cliente exige repetir el identificador y elimina sus datos asociados en la base activa. Los backups cifrados conservados pueden contener datos anteriores hasta que expire su retención. Las consultas de [METRICS.sql](METRICS.sql) dan métricas por negocio; el abandono se aproxima mediante carritos que quedaron en borrador, no mide intención conversacional real.

Rotación de MASTER_KEY: detener escrituras, descifrar credenciales con la antigua, volver a cifrar con la nueva en una transacción administrativa, cambiar el secreto de ejecución y reiniciar app/worker. Conservar la clave anterior solamente mientras se necesite restaurar backups previos; jamás registrar ninguna clave ni ponerla en Git.

## Límites honestos

Usar datos y números ficticios. No utilizar el número real de un cliente para WaSender de prueba. No hay envíos reales ni conversación generativa. El detector de texto sensible no reemplaza una política de datos. Las métricas comerciales del documento (interés, clientes que pagan, costo real, abandono tras seis semanas) dependen del piloto y no pueden validarse con semillas.
