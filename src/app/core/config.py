"""Explicit configuration; importing modules opens no connections.

Sources, highest first: constructor arguments, ``BATCH_*`` environment
variables, ``.env``, ``config.{APP_ENV}.yml`` (values for one environment,
when ``APP_ENV`` is set), then ``config.yml`` (values shared by every
environment, when present), both at the project root. ConfigMaps and
Secrets are unavailable on the platform, so the files ship in the image.
"""

import os
from pathlib import Path
from typing import Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from sqlalchemy.engine import make_url

PROJECT_ROOT = Path(__file__).resolve().parents[3]
APP_ENVS = ("dev", "stg", "prd")


def config_files() -> list[Path]:
    """YAML settings files, highest precedence first."""
    files = []
    app_env = os.environ.get("APP_ENV")
    if app_env:
        if app_env not in APP_ENVS:
            raise ValueError(f"APP_ENV must be one of {', '.join(APP_ENVS)}.")
        path = PROJECT_ROOT / f"config.{app_env}.yml"
        if not path.is_file():
            raise FileNotFoundError(f"APP_ENV={app_env} needs {path}.")
        files.append(path)
    common = PROJECT_ROOT / "config.yml"
    if common.is_file():
        files.append(common)
    return files


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BATCH_", env_file=".env", extra="ignore"
    )

    # Bind address for the pr-batch entry point (PORT in config.yml).
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: SecretStr
    storage_root: Path
    auth_secret: SecretStr
    auth_issuer: str = "prj_re"
    auth_audience: str = "pr-batch"
    auth_max_lifetime_seconds: int = Field(default=300, ge=30, le=3600)
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_code_cells: int = Field(default=100, ge=1, le=10000)
    cell_max_bytes: int = Field(default=1024 * 1024, ge=1)
    # Remote calls never hold a connection, so a small pool suffices.
    db_pool_size: int = Field(default=3, ge=1, le=100)
    db_max_overflow: int = Field(default=3, ge=0, le=100)
    # Kubernetes allows neither Jobs nor init containers for this service:
    # the first pod upgrades the schema under a PostgreSQL advisory lock.
    db_auto_migrate: bool = True

    executor_base_url: str | None = None
    # BATCH runtimes come from the Executor; cache them this long.
    runtime_catalog_ttl_seconds: float = Field(default=30, ge=1, le=3600)

    # In-process background runtime (scheduler + task dispatcher).
    background_enabled: bool = True
    task_concurrency: int = Field(default=4, ge=1, le=64)
    task_lease_seconds: int = Field(default=90, ge=30, le=3600)
    idle_max_seconds: float = Field(default=5, gt=0, le=60)
    sync_initial_seconds: int = Field(default=5, ge=1, le=300)
    sync_max_seconds: int = Field(default=60, ge=1, le=3600)
    shutdown_grace_seconds: float = Field(default=40, ge=1, le=600)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        files = [
            YamlConfigSettingsSource(settings_cls, yaml_file=path)
            for path in config_files()
        ]
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            *files,
            file_secret_settings,
        )

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        url = make_url(self.database_url.get_secret_value())
        if url.drivername != "postgresql+psycopg":
            raise ValueError("Use a postgresql+psycopg database URL.")
        if not self.storage_root.is_absolute():
            raise ValueError("storage_root must be an absolute path.")
        if len(self.auth_secret.get_secret_value().encode()) < 32:
            raise ValueError("auth_secret must contain at least 32 bytes.")
        if self.executor_base_url is not None:
            endpoint = urlsplit(self.executor_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError(
                    "executor_base_url: use an HTTP base URL without "
                    "credentials."
                )
        if self.sync_initial_seconds > self.sync_max_seconds:
            raise ValueError("sync_initial_seconds exceeds sync_max_seconds.")
        return self
