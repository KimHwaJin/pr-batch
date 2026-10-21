"""Settings layering: environment over .env over config.{APP_ENV}.yml."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import config
from app.core.config import Settings

SECRET = "y" * 32


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name in (
        "APP_ENV",
        "BATCH_DATABASE_URL",
        "BATCH_STORAGE_ROOT",
        "BATCH_AUTH_SECRET",
        "BATCH_DB_POOL_SIZE",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_config(root: Path, env: str, pool_size: int = 7) -> None:
    (root / f"config.{env}.yml").write_text(
        "database_url: postgresql+psycopg://u:p@db:5432/batch\n"
        f"storage_root: {root}\n"
        f"auth_secret: {SECRET}\n"
        f"db_pool_size: {pool_size}\n"
    )


def test_reads_the_app_env_file(project, monkeypatch):
    write_config(project, "stg")
    monkeypatch.setenv("APP_ENV", "stg")
    settings = Settings(_env_file=None)
    assert settings.db_pool_size == 7
    assert settings.storage_root == project
    assert settings.auth_secret.get_secret_value() == SECRET


def test_keys_are_case_insensitive(project, monkeypatch):
    (project / "config.dev.yml").write_text(
        "DATABASE_URL: postgresql+psycopg://u:p@db:5432/batch\n"
        f"STORAGE_ROOT: {project}\n"
        f"AUTH_SECRET: {SECRET}\n"
        "DB_POOL_SIZE: 9\n"
    )
    monkeypatch.setenv("APP_ENV", "dev")
    assert Settings(_env_file=None).db_pool_size == 9


def test_environment_overrides_the_file(project, monkeypatch):
    write_config(project, "prd")
    monkeypatch.setenv("APP_ENV", "prd")
    monkeypatch.setenv("BATCH_DB_POOL_SIZE", "2")
    assert Settings(_env_file=None).db_pool_size == 2


def test_no_app_env_ignores_files(project):
    write_config(project, "dev")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_unknown_app_env_fails(project, monkeypatch):
    monkeypatch.setenv("APP_ENV", "../dev")
    with pytest.raises(ValueError, match="APP_ENV must be one of"):
        Settings(_env_file=None)


def test_missing_file_fails(project, monkeypatch):
    monkeypatch.setenv("APP_ENV", "dev")
    with pytest.raises(FileNotFoundError, match=r"config\.dev\.yml"):
        Settings(_env_file=None)
