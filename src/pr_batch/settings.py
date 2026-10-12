"""Explicit configuration; importing modules opens no connections."""

from pathlib import Path
from typing import Self

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
    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)

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
        return self
