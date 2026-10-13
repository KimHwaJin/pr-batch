"""Explicit configuration; importing modules opens no connections."""

from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BATCH_", env_file=".env", extra="ignore"
    )

    database_url: SecretStr
    storage_root: Path
    auth_secret: SecretStr
    auth_issuer: str = "prj_re"
    auth_audience: str = "pr-batch"
    auth_max_lifetime_seconds: int = Field(default=300, ge=30, le=3600)
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_code_cells: int = Field(default=100, ge=1, le=10000)
    cell_max_bytes: int = Field(default=1024 * 1024, ge=1)
    allowed_kernel_profiles: list[str] = Field(min_length=1)
    # Remote calls never hold a connection, so a small pool suffices.
    db_pool_size: int = Field(default=3, ge=1, le=100)
    db_max_overflow: int = Field(default=3, ge=0, le=100)
    # Kubernetes allows neither Jobs nor init containers for this service:
    # the first pod upgrades the schema under a PostgreSQL advisory lock.
    db_auto_migrate: bool = True

    executor_base_url: str | None = None
    # prj_re report API; when unset, successful runs complete unreported.
    report_base_url: str | None = None
    report_token_secret: SecretStr | None = None
    report_token_audience: str = "prj_re-report"

    # In-process background runtime (scheduler + task dispatcher).
    background_enabled: bool = True
    task_concurrency: int = Field(default=4, ge=1, le=64)
    task_lease_seconds: int = Field(default=90, ge=30, le=3600)
    idle_max_seconds: float = Field(default=5, gt=0, le=60)
    sync_initial_seconds: int = Field(default=5, ge=1, le=300)
    sync_max_seconds: int = Field(default=60, ge=1, le=3600)
    shutdown_grace_seconds: float = Field(default=40, ge=1, le=600)

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg":
            raise ValueError("Use a postgresql+psycopg database URL.")
        if not self.storage_root.is_absolute():
            raise ValueError("storage_root must be an absolute path.")
        if len(self.auth_secret.get_secret_value().encode()) < 32:
            raise ValueError("auth_secret must contain at least 32 bytes.")
        if any(not name.strip() for name in self.allowed_kernel_profiles):
            raise ValueError("Kernel profile names must not be blank.")
        for name in ("executor_base_url", "report_base_url"):
            value = getattr(self, name)
            if value is None:
                continue
            endpoint = urlsplit(value)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    f"{name}: use an HTTP base URL without credentials."
                )
        if self.report_base_url is not None:
            secret = self.report_token_secret
            if secret is None or len(secret.get_secret_value().encode()) < 32:
                raise ValueError(
                    "report_token_secret (32+ bytes) is required with "
                    "report_base_url."
                )
        if self.sync_initial_seconds > self.sync_max_seconds:
            raise ValueError("sync_initial_seconds exceeds sync_max_seconds.")
        return self
