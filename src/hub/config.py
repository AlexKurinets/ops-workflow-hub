from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def default_workflows_dir() -> Path:
    """The repo's workflows folder in a checkout, or ./workflows when installed in a container."""
    candidate = REPO_ROOT / "workflows"
    return candidate if candidate.is_dir() else Path.cwd() / "workflows"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/hub"
    workflows_dir: Path = Field(default_factory=default_workflows_dir)

    webhook_secret: str = "change-me"
    webhook_secret_ats: str | None = None
    webhook_secret_esign: str | None = None
    webhook_secret_crm: str | None = None
    webhook_secret_forms: str | None = None

    slack_mode: str = Field(default="sandbox", pattern="^(sandbox|console|webhook)$")
    slack_webhook_url: str | None = None
    slack_alert_channel: str = "#ops-alerts"

    email_mode: str = Field(default="sandbox", pattern="^(sandbox|console|smtp)$")
    smtp_host: str | None = None
    smtp_port: int = 587

    public_base_url: str = "http://localhost:8000"

    worker_id: str = "worker-1"
    worker_poll_seconds: float = 1.0
    scheduler_poll_seconds: float = 5.0

    def webhook_secret_for(self, source: str) -> str:
        specific = getattr(self, f"webhook_secret_{source}", None)
        return specific or self.webhook_secret


@lru_cache
def get_settings() -> Settings:
    return Settings()
