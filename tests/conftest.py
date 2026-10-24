from datetime import UTC, datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.admin import admin_connection, seed_demo
from app.clock import FakeClock
from app.db import migrate
from app.runtime import Runtime
from app.settings import Settings


@pytest.fixture(scope="session")
def database_settings():
    settings = Settings(app_env="test", dev_routes=False)
    name = "barrio_test_" + uuid4().hex[:12]
    with psycopg.connect(settings.database_url_admin, autocommit=True, connect_timeout=5) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    admin = settings.database_url_admin.rsplit("/", 1)[0] + "/" + name
    app = settings.database_url_app.rsplit("/", 1)[0] + "/" + name
    backup = settings.database_url_backup.rsplit("/", 1)[0] + "/" + name
    settings = settings.model_copy(
        update={"database_url_admin": admin, "database_url_app": app, "database_url_backup": backup}
    )
    migrate(settings)
    yield settings
    with psycopg.connect(admin.rsplit("/", 1)[0] + "/postgres", autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def rt(database_settings, tmp_path):
    with admin_connection(database_settings) as c:
        c.execute("TRUNCATE tenants CASCADE")
    clock = FakeClock(datetime(2026, 10, 26, 16, 0, tzinfo=UTC))
    runtime = Runtime(database_settings.model_copy(update={"runtime_dir": tmp_path}), clock)
    seed_demo(runtime)
    yield runtime
    runtime.close()


@pytest.fixture
def salon(rt):
    from app.admin import demo_tokens

    return demo_tokens(rt)["salon-ejemplo"]


@pytest.fixture
def tacos(rt):
    from app.admin import demo_tokens

    return demo_tokens(rt)["taqueria-ejemplo"]
