import os
import subprocess
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlsplit, urlunsplit

import psycopg
from cryptography.fernet import Fernet

from app.adapters import local_key
from app.errors import NotConfigured


def pg_command(rt, command):
    local = rt.settings.runtime_dir / "postgresql/pgsql/bin" / (command + ".exe")
    if not local.exists():
        local = Path(".runtime/postgresql/pgsql/bin") / (command + ".exe")
    return str(local.resolve()) if local.exists() else command


def database_argument(url):
    parsed = urlsplit(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port:
        host += f":{parsed.port}"
    if parsed.username:
        host = parsed.username + "@" + host
    env = os.environ.copy()
    if parsed.password:
        env["PGPASSWORD"] = unquote(parsed.password)
    return urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)), env


def backup_key(rt):
    if rt.settings.backup_encryption_key:
        return rt.settings.backup_encryption_key.encode()
    if rt.settings.app_env == "prod":
        raise NotConfigured("Falta BACKUP_ENCRYPTION_KEY")
    return local_key(rt.settings.runtime_dir / "backup.key")


def create_backup(rt):
    url, env = database_argument(rt.settings.database_url_backup)
    result = subprocess.run(
        [pg_command(rt, "pg_dump"), "--dbname", url, "--format=custom", "--no-owner"],
        env=env,
        capture_output=True,
        check=True,
    )
    encrypted = Fernet(backup_key(rt)).encrypt(result.stdout)
    name = "barrio-" + rt.clock.now().strftime("%Y%m%dT%H%M%SZ") + ".dump.enc"
    target = rt.backup.put(name, encrypted)
    if rt.settings.backup_adapter == "local":
        directory = (rt.settings.runtime_dir / "backups").resolve()
        for path in directory.glob("barrio-*.dump.enc"):
            if (
                path.resolve().parent == directory
                and path.stat().st_mtime < (rt.clock.now() - timedelta(days=14)).timestamp()
            ):
                path.unlink()
    return target


def restore_backup(rt, name, destination_url):
    if destination_url == rt.settings.database_url_admin:
        raise ValueError("Restaurar exige una base nueva; no se sobrescribe la base activa")
    url, env = database_argument(destination_url)
    with psycopg.connect(destination_url) as c:
        if c.execute("SELECT 1 FROM pg_tables WHERE schemaname='public' LIMIT 1").fetchone():
            raise ValueError("La base de destino debe estar vacía")
    data = Fernet(backup_key(rt)).decrypt(rt.backup.get(Path(name).name))
    subprocess.run(
        [
            pg_command(rt, "pg_restore"),
            "--dbname",
            url,
            "--no-owner",
            "--exit-on-error",
            "--single-transaction",
        ],
        input=data,
        env=env,
        capture_output=True,
        check=True,
    )
