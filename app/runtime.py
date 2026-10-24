from app.adapters import DevSecretStore, EnvSecretStore, FakeNotifier, LocalBackupTarget
from app.backup.s3 import S3BackupTarget
from app.channels.fake import FakeChannel
from app.channels.wasender import WaSenderChannel
from app.clock import SystemClock
from app.db import Database
from app.llm.openai import OpenAILLM
from app.llm.scripted import ScriptedLLM
from app.notify.email import EmailNotifier
from app.notify.telegram import TelegramNotifier
from app.settings import Settings


class FallbackNotifier:
    def __init__(self):
        self.primary, self.fallback = TelegramNotifier(), EmailNotifier()

    def send(self, notification):
        try:
            self.primary.send(notification)
        except (RuntimeError, ConnectionError, TimeoutError):
            self.fallback.send(notification)


class Runtime:
    def __init__(self, settings=None, clock=None):
        self.settings = settings or Settings()
        self.clock = clock or SystemClock()
        self.db = Database(self.settings)
        self.channel = (
            FakeChannel(self.db, self.clock) if self.settings.channel_adapter == "fake" else WaSenderChannel()
        )
        self.llm = ScriptedLLM() if self.settings.llm_adapter == "fake" else OpenAILLM()
        if self.settings.llm_adapter == "fake":
            self.llm.failure_mode = self.settings.fake_llm_failure
        self.notifier = {
            "fake": lambda: FakeNotifier(self.db, self.clock),
            "telegram": TelegramNotifier,
            "email": EmailNotifier,
            "telegram,email": FallbackNotifier,
        }[self.settings.notifier_adapter]()
        if self.settings.notifier_adapter == "fake":
            self.notifier.failures_left = self.settings.fake_notifier_failures
        self.secrets = (
            DevSecretStore(self.settings.runtime_dir)
            if self.settings.secrets_adapter == "dev"
            else EnvSecretStore(self.settings.master_key)
        )
        self.backup = (
            LocalBackupTarget(self.settings.runtime_dir / "backups")
            if self.settings.backup_adapter == "local"
            else S3BackupTarget()
        )

    def close(self):
        self.db.close()
