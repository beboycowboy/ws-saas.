from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.settings import Settings


def migrate(settings: Settings):
    with psycopg.connect(settings.database_url_admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute("SELECT pg_advisory_lock(772241)")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY)")
            for path in sorted(Path(__file__).parent.glob("*.sql")):
                if not conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version=%s", (path.name,)
                ).fetchone():
                    with conn.transaction():
                        conn.execute(path.read_text(encoding="utf-8"))
                        conn.execute("INSERT INTO schema_migrations VALUES (%s)", (path.name,))
        finally:
            conn.execute("SELECT pg_advisory_unlock(772241)")


class Database:
    def __init__(self, settings: Settings):
        self.pool = ConnectionPool(
            settings.database_url_app,
            min_size=1,
            max_size=24,
            kwargs={
                "row_factory": dict_row,
                "autocommit": True,
                "options": "-c timezone=UTC",
                "connect_timeout": 5,
            },
            open=True,
        )
        self.pool.wait(timeout=10)
        with self.pool.connection() as c:
            role = c.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname=current_user"
            ).fetchone()
            if role["rolsuper"] or role["rolbypassrls"]:
                self.close()
                raise RuntimeError("DATABASE_URL_APP debe usar un rol sin superuser/BYPASSRLS")

    @contextmanager
    def tx(self, tenant_id, connection=None):
        tenant_id = str(UUID(str(tenant_id)))
        if connection is not None:
            with connection.transaction():
                connection.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
                yield connection
        else:
            with self.pool.connection() as conn, conn.transaction():
                conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))
                yield conn

    def resolve(self, kind, token_hash):
        functions = {"channel": "resolve_channel", "agenda": "resolve_agenda", "link": "resolve_link"}
        with self.pool.connection() as conn:
            return conn.execute(f"SELECT * FROM {functions[kind]}(%s)", (token_hash,)).fetchone()

    def tenant_ids(self):
        with self.pool.connection() as conn:
            return [r["id"] for r in conn.execute("SELECT * FROM work_tenants()")]

    def close(self):
        self.pool.close()
