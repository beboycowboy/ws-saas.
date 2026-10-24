import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet
from psycopg.types.json import Jsonb

from app.errors import NotConfigured
from app.logging import event


class FakeNotifier:
    def __init__(self, db, clock):
        self.db, self.clock = db, clock
        self.failures_left = 0

    def send(self, notification):
        if self.failures_left:
            self.failures_left -= 1
            raise ConnectionError("Simulated notifier failure")
        with self.db.tx(notification["tenant_id"]) as c:
            c.execute(
                """INSERT INTO fake_notifications(tenant_id,outbox_id,type,payload_json,created_at)
                VALUES (%s,%s,%s,%s,%s) ON CONFLICT(outbox_id) DO NOTHING""",
                (
                    notification["tenant_id"],
                    notification["id"],
                    notification["type"],
                    Jsonb(notification["payload_json"]),
                    self.clock.now(),
                ),
            )
        event("notification_delivered", tenant_id=notification["tenant_id"])


def local_key(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_bytes()
    # Publish only a complete key; app and worker may initialize concurrently.
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix="key-", delete=False) as file:
        temporary = Path(file.name)
        file.write(Fernet.generate_key())
    try:
        os.chmod(temporary, 0o600)
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
    finally:
        temporary.unlink()
    return path.read_bytes()


class DevSecretStore:
    def __init__(self, directory: Path):
        self.fernet = Fernet(local_key(directory / "master.key"))

    def encrypt(self, value):
        return self.fernet.encrypt(value.encode()).decode()

    def decrypt(self, value):
        return self.fernet.decrypt(value.encode()).decode()


class EnvSecretStore(DevSecretStore):
    def __init__(self, key):
        if not key:
            raise NotConfigured("SecretStore env: falta MASTER_KEY (clave Fernet)")
        self.fernet = Fernet(key.encode())


class LocalBackupTarget:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name):
        if Path(name).name != name or name in {".", ".."} or ":" in name:
            raise ValueError("Nombre de backup inválido")
        return self.directory / name

    def put(self, name, data):
        path = self._path(name)
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
        return str(path)

    def get(self, name):
        return self._path(name).read_bytes()
