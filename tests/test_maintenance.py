from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from app.backup.service import create_backup, restore_backup
from app.db import Database
from app.maintenance import erase_customer, purge_tenant
from app.sim import drain, send

pytestmark = pytest.mark.integration


def test_encrypted_backup_restores_data_rls_and_runtime_permissions(rt, salon):
    name = "barrio_restore_" + uuid4().hex[:12]
    admin = rt.settings.database_url_admin.rsplit("/", 1)[0] + "/" + name
    app = rt.settings.database_url_app.rsplit("/", 1)[0] + "/" + name
    path = Path(create_backup(rt))
    assert path.read_bytes().startswith(b"gAAAA")
    with psycopg.connect(rt.settings.database_url_admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        restore_backup(rt, path.name, admin)
        db = Database(rt.settings.model_copy(update={"database_url_app": app}))
        try:
            assert len(db.tenant_ids()) == 3
            with db.tx(salon["tenant_id"]) as c:
                assert c.execute("SELECT count(*) n FROM appointments").fetchone()["n"] == 1
                assert c.execute("SELECT count(*) n FROM tenants").fetchone()["n"] == 1
            with db.pool.connection() as c:
                assert c.execute("SELECT count(*) n FROM appointments").fetchone()["n"] == 0
        finally:
            db.close()
        with pytest.raises(ValueError, match="vacía"):
            restore_backup(rt, path.name, admin)
    finally:
        with psycopg.connect(rt.settings.database_url_admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_customer_erasure_includes_messages_orders_and_notifications(rt, tacos):
    for message in ["agregar taco_pastor 5", "pedido delivery Calle Privada 123", "confirmar"]:
        send(rt, "taqueria-ejemplo", "erase-me", message)
        drain(rt)
    assert erase_customer(rt, tacos["tenant_id"], "erase-me")
    with rt.db.tx(tacos["tenant_id"]) as c:
        assert not c.execute("SELECT 1 FROM customers WHERE contact_id='erase-me'").fetchone()
        assert not c.execute("SELECT 1 FROM messages WHERE text LIKE '%Calle Privada%'").fetchone()
        assert not c.execute("SELECT 1 FROM orders WHERE address_note LIKE '%Calle Privada%'").fetchone()
        assert not c.execute("SELECT 1 FROM fake_channel_log WHERE contact_id='erase-me'").fetchone()
        assert c.execute("SELECT count(*) n FROM orders").fetchone()["n"] == 1  # unrelated seed survives


def test_retention_removes_derived_message_copies(rt, salon):
    send(rt, "salon-ejemplo", "retention", "hola")
    drain(rt)
    rt.clock.advance(91 * 86400)
    with rt.db.tx(salon["tenant_id"]) as c:
        assert purge_tenant(c, rt.clock.now()) >= 2
        assert not c.execute("SELECT 1 FROM fake_channel_log WHERE contact_id='retention'").fetchone()
        assert not c.execute("SELECT 1 FROM outbound").fetchone()
