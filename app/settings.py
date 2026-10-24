from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    app_env: Literal["dev", "test", "prod"] = "dev"
    database_url_app: str = "postgresql://ws_app@127.0.0.1:5432/barrio"
    database_url_admin: str = "postgresql://postgres@127.0.0.1:5432/barrio"
    database_url_backup: str = "postgresql://ws_backup@127.0.0.1:5432/barrio"
    public_base_url: str = "http://127.0.0.1:8000"
    channel_adapter: Literal["fake", "wasender"] = "fake"
    llm_adapter: Literal["fake", "openai"] = "fake"
    notifier_adapter: Literal["fake", "telegram", "email", "telegram,email"] = "fake"
    secrets_adapter: Literal["dev", "env"] = "dev"
    backup_adapter: Literal["local", "s3"] = "local"
    dev_routes: bool | None = None
    master_key: str | None = None
    backup_encryption_key: str | None = None
    runtime_dir: Path = Path(".runtime")
    feature_owner_triggered_replies: bool = False
    feature_owner_notify_whatsapp: bool = False
    llm_turn_max_tokens: int = Field(default=2000, ge=1, le=16000)
    llm_conversation_max_tokens: int = Field(default=20000, ge=1)
    llm_daily_token_budget_per_tenant: int = Field(default=100000, ge=1)
    llm_timeout_seconds: float = Field(default=25, gt=0, le=25)
    fake_llm_failure: Literal["timeout", "error", "invalid"] | None = None
    fake_notifier_failures: int = Field(default=0, ge=0)
    rate_limits_contact_hour: int = Field(default=20, ge=1)
    rate_limits_account_hour: int = Field(default=150, ge=1)
    rate_limits_account_day: int = Field(default=800, ge=1)
    queue_max_pending: int = Field(default=1000, ge=1)
    debounce_seconds: float = Field(default=2.5, ge=0, le=10)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_environment(self):
        if self.dev_routes is None:
            self.dev_routes = self.app_env == "dev"
        if self.app_env != "dev" and self.dev_routes:
            raise ValueError("Las rutas /dev solo se permiten con APP_ENV=dev")
        if self.feature_owner_notify_whatsapp:
            raise ValueError("OWNER_NOTIFY_WHATSAPP queda pendiente del piloto; no está conectado")
        if self.app_env == "prod" and (
            self.channel_adapter == "fake"
            or self.llm_adapter == "fake"
            or self.notifier_adapter == "fake"
            or self.secrets_adapter == "dev"
            or self.backup_adapter == "local"
        ):
            raise ValueError("Producción rechaza adaptadores fake/dev/local")
        return self
