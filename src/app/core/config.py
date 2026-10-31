"""Explicit configuration; importing modules opens no connections.

Sources, highest first: constructor arguments, environment variables
named after the fields (no prefix, any case), ``.env``,
``config.{APP_ENV}.yml`` (values for one environment, when ``APP_ENV``
is set), then ``config.yml`` (values shared by every environment, when
present), both at the project root. ConfigMaps and
Secrets are unavailable on the platform, so the files ship in the image.
"""

import hashlib
import hmac
import os
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)
from sqlalchemy.engine import make_url

from app.schemas.batch import RuntimeSpec
from app.schemas.common import TenantId

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


# Environments where identity headers may be trusted without a key.
HEADER_AUTH_ENVS = (None, "dev")

ClientName = Annotated[
    str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}[a-z0-9]$")
]
KeyHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class AuthClient(BaseModel):
    """One calling service, known by the SHA-256 of its API key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: ClientName
    # Several hashes while a key is being replaced.
    key_sha256: list[KeyHash] = Field(min_length=1)
    # Event Stream for batches this caller registers.
    tenant: TenantId
    # A relay may name another tenant (X-Tenant-Id), within allowed_tenants.
    relay: bool = False
    allowed_tenants: list[TenantId] = Field(default_factory=list)
    # May call owner-wide operations such as disable-by-owner.
    admin: bool = False

    @model_validator(mode="after")
    def validate_relay(self) -> Self:
        if self.allowed_tenants and not self.relay:
            raise ValueError(f"{self.name}: allowed_tenants needs relay.")
        return self


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Bind address for the pr-batch entry point (PORT in config.yml).
    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    database_url: SecretStr
    storage_root: Path
    # api_key: callers send X-Api-Key, matched against auth_clients.
    # header: identity headers are trusted without a key (local/dev only).
    auth_mode: Literal["api_key", "header"] = "api_key"
    auth_clients: list[AuthClient] = Field(default_factory=list)
    upload_max_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_code_cells: int = Field(default=100, ge=1, le=10000)
    cell_max_bytes: int = Field(default=1024 * 1024, ge=1)
    # Remote calls never hold a connection, so a small pool suffices.
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)
    # Kubernetes allows neither Jobs nor init containers for this service:
    # the first pod upgrades the schema under a PostgreSQL advisory lock.
    db_auto_migrate: bool = True

    executor_base_url: str | None = None
    # pr-batch's own key for the Executor (X-Api-Key on every call).
    executor_api_key: SecretStr | None = None
    # Runtimes batches may choose; kernels of the Executor's BATCH pool.
    batch_runtimes: list[RuntimeSpec] = Field(default_factory=list)

    # In-process background runtime (scheduler + task dispatcher).
    background_enabled: bool = True
    task_concurrency: int = Field(default=4, ge=1, le=64)
    task_lease_seconds: int = Field(default=90, ge=30, le=3600)
    idle_max_seconds: float = Field(default=5, gt=0, le=60)
    sync_initial_seconds: int = Field(default=5, ge=1, le=300)
    sync_max_seconds: int = Field(default=60, ge=1, le=3600)
    shutdown_grace_seconds: float = Field(default=40, ge=1, le=600)
    # A run whose Executor calls keep failing this long becomes FAILED
    # (failure_type ABANDONED) instead of holding its slot forever.
    task_give_up_seconds: int = Field(default=86400, ge=600, le=2592000)
    # Purge expired idempotency keys and settled tasks (history is kept).
    idempotency_retention_days: int = Field(default=7, ge=1, le=365)
    task_retention_days: int = Field(default=30, ge=1, le=365)

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
        if (
            self.auth_mode == "header"
            and os.environ.get("APP_ENV") not in HEADER_AUTH_ENVS
        ):
            raise ValueError("auth_mode header is only allowed locally/dev.")
        hashes = [h for c in self.auth_clients for h in c.key_sha256]
        if len(hashes) != len(set(hashes)):
            raise ValueError("auth_clients: a key hash is listed twice.")
        names = [c.name for c in self.auth_clients]
        if len(names) != len(set(names)):
            raise ValueError("auth_clients: a client name is listed twice.")
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

    def match_client(self, key: str) -> AuthClient | None:
        """The client whose key this is; constant time per stored hash."""
        digest = hashlib.sha256(key.encode()).hexdigest()
        found = None
        for client in self.auth_clients:
            for stored in client.key_sha256:
                if hmac.compare_digest(digest, stored):
                    found = client
        return found
