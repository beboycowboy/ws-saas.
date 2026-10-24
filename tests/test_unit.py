from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.adapters import DevSecretStore, EnvSecretStore, LocalBackupTarget
from app.backup.s3 import S3BackupTarget
from app.channels.wasender import WaSenderChannel
from app.clock import FakeClock
from app.config import BusinessConfig, ServiceConfig
from app.core.conversation import typing_delay
from app.core.ingest import sensitive
from app.core.scheduling import candidates, local_instant
from app.errors import BusinessError, NotConfigured
from app.llm.openai import OpenAILLM
from app.notify.email import EmailNotifier
from app.notify.telegram import TelegramNotifier
from app.settings import Settings


def test_production_rejects_all_local_adapters():
    with pytest.raises(ValidationError, match="Producción"):
        Settings(app_env="prod")
    with pytest.raises(ValidationError, match="/dev"):
        Settings(app_env="test", dev_routes=True)
    real = dict(
        app_env="prod",
        channel_adapter="wasender",
        llm_adapter="openai",
        notifier_adapter="telegram,email",
        secrets_adapter="env",
        backup_adapter="s3",
    )
    for name, value in [
        ("channel_adapter", "fake"),
        ("llm_adapter", "fake"),
        ("notifier_adapter", "fake"),
        ("secrets_adapter", "dev"),
        ("backup_adapter", "local"),
    ]:
        with pytest.raises(ValidationError):
            Settings(**{**real, name: value})


def test_real_adapters_fail_without_network():
    channel = WaSenderChannel()
    calls = [
        lambda: channel.parse_inbound(None),
        lambda: channel.send_text({}, "x", "text"),
        lambda: channel.send_typing({}, "x"),
        lambda: channel.session_status({}),
        lambda: OpenAILLM().generate([], [], 100, 0),
        lambda: TelegramNotifier().send({}),
        lambda: TelegramNotifier().parse_action(b"", {}),
        lambda: EmailNotifier().send({}),
        lambda: EnvSecretStore(None),
        lambda: S3BackupTarget().put("x", b""),
        lambda: S3BackupTarget().get("x"),
    ]
    for call in calls:
        with pytest.raises(NotConfigured):
            call()


def test_secret_store_and_backup(tmp_path):
    store = DevSecretStore(tmp_path)
    encoded = store.encrypt("private")
    assert "private" not in encoded
    assert DevSecretStore(tmp_path).decrypt(encoded) == "private"
    target = LocalBackupTarget(tmp_path / "backups")
    target.put("test.bin", b"data")
    assert target.get("test.bin") == b"data"
    with pytest.raises(ValueError):
        target.get("../test.bin")


def test_dst_rejects_nonexistent_and_ambiguous_local_times():
    zone = ZoneInfo("America/New_York")
    for value in ["2026-03-08T02:30:00", "2026-11-01T01:30:00"]:
        with pytest.raises(BusinessError):
            local_instant(value, zone)
    assert local_instant("2026-11-01T01:30:00-04:00", zone).hour == 5


def test_clock_and_delay():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(60)
    assert clock.now().minute == 1
    assert all(0 <= typing_delay("x" * 10000) <= 8 for _ in range(20))


def test_money_and_injection_fields_rejected():
    with pytest.raises(ValidationError):
        ServiceConfig(code="corte", name="Corte", minutes=30, price_mxn="10.999")
    from app.core.tools import AddItem

    with pytest.raises(ValidationError):
        AddItem(item_code="x", qty=1, price_cents=1, tenant_id="other")
    with pytest.raises(ValidationError):
        AddItem(item_code="x", qty=True)


def test_sensitive_text_detection():
    assert sensitive("mi diagnóstico es privado")
    assert sensitive("mi tarjeta 4111 1111 1111 1111")
    assert not sensitive("quiero cinco tacos")


def test_scheduling_closing_buffer_and_closed_date():
    config = BusinessConfig.model_validate(
        {
            "tenant": {"slug": "test", "name": "Test", "owner": {"email": "a@example.test"}},
            "appointments": {"weekly_hours": {"mon": [["10:00", "11:00"]]}, "closed_dates": ["2026-11-02"]},
        }
    )
    service = {"minutes": 45, "buffer_minutes": 5}
    now = datetime(2026, 10, 26, 12, tzinfo=UTC)
    day = now.date()
    slots = list(candidates(config, service, day, day, now))
    assert len(slots) == 1
    assert not list(
        candidates(config, service, day.replace(month=11, day=2), day.replace(month=11, day=2), now)
    )
