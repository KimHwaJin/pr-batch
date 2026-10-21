"""Settings layering: environment over .env over config.{APP_ENV}.yml."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PUBLIC_PEM
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from pydantic import ValidationError

from app.core import config
from app.core.config import Settings


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "auth_public.pem").write_bytes(PUBLIC_PEM)
    for name in (
        "APP_ENV",
        "BATCH_DATABASE_URL",
        "BATCH_STORAGE_ROOT",
        "BATCH_AUTH_PUBLIC_KEY_FILE",
        "BATCH_DB_POOL_SIZE",
        "BATCH_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_config(root: Path, env: str, pool_size: int = 7) -> None:
    (root / f"config.{env}.yml").write_text(
        "database_url: postgresql+psycopg://u:p@db:5432/batch\n"
        f"storage_root: {root}\n"
        "auth_public_key_file: keys/auth_public.pem\n"
        f"db_pool_size: {pool_size}\n"
    )


def test_reads_the_app_env_file(project, monkeypatch):
    write_config(project, "stg")
    monkeypatch.setenv("APP_ENV", "stg")
    settings = Settings(_env_file=None)
    assert settings.db_pool_size == 7
    assert settings.storage_root == project
    assert (
        settings.auth_public_key.public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        == PUBLIC_PEM
    )


def test_keys_are_case_insensitive(project, monkeypatch):
    (project / "config.dev.yml").write_text(
        "DATABASE_URL: postgresql+psycopg://u:p@db:5432/batch\n"
        f"STORAGE_ROOT: {project}\n"
        "AUTH_PUBLIC_KEY_FILE: keys/auth_public.pem\n"
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


def test_common_file_applies_and_environment_file_wins(project, monkeypatch):
    (project / "config.yml").write_text(
        "database_url: postgresql+psycopg://u:p@db:5432/batch\n"
        f"storage_root: {project}\n"
        "auth_public_key_file: keys/auth_public.pem\n"
        "db_pool_size: 5\n"
        "task_concurrency: 6\n"
    )
    settings = Settings(_env_file=None)
    assert (settings.db_pool_size, settings.task_concurrency) == (5, 6)
    (project / "config.prd.yml").write_text("db_pool_size: 8\n")
    monkeypatch.setenv("APP_ENV", "prd")
    settings = Settings(_env_file=None)
    assert (settings.db_pool_size, settings.task_concurrency) == (8, 6)


def test_port_comes_from_config_yml(project, monkeypatch):
    write_config(project, "dev")
    (project / "config.yml").write_text("PORT: 9100\n")
    monkeypatch.setenv("APP_ENV", "dev")
    assert Settings(_env_file=None).port == 9100


def test_entry_point_serves_on_configured_port(monkeypatch):
    from app import serve

    calls = {}
    monkeypatch.setattr(
        serve, "Settings", lambda: SimpleNamespace(host="127.0.0.1", port=9200)
    )
    monkeypatch.setattr(
        serve.uvicorn, "run", lambda app, **kwargs: calls.update(kwargs)
    )
    serve.run()
    assert calls == {"factory": True, "host": "127.0.0.1", "port": 9200}


def pem(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.mark.parametrize(
    "content",
    [
        None,
        b"not a key",
        pem(rsa.generate_private_key(public_exponent=65537, key_size=1024)),
        pem(ec.generate_private_key(ec.SECP256R1())),
    ],
)
def test_public_key_file_must_hold_an_rsa_key(project, monkeypatch, content):
    write_config(project, "dev")
    key_file = project / "keys" / "auth_public.pem"
    if content is None:
        key_file.unlink()
    else:
        key_file.write_bytes(content)
    monkeypatch.setenv("APP_ENV", "dev")
    with pytest.raises(ValidationError, match="auth_public_key_file"):
        Settings(_env_file=None)
