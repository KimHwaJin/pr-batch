"""Settings layering: environment over .env over config.{APP_ENV}.yml."""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.core import config
from app.core.config import Settings


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    for name in (
        "APP_ENV",
        "DATABASE_URL",
        "STORAGE_ROOT",
        "AUTH_MODE",
        "AUTH_CLIENTS",
        "DB_POOL_SIZE",
        "PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_config(root: Path, env: str, pool_size: int = 7) -> None:
    (root / f"config.{env}.yml").write_text(
        "database_url: postgresql+psycopg://u:p@db:5432/batch\n"
        f"storage_root: {root}\n"
        f"db_pool_size: {pool_size}\n"
    )


def test_reads_the_app_env_file(project, monkeypatch):
    write_config(project, "stg")
    monkeypatch.setenv("APP_ENV", "stg")
    settings = Settings(_env_file=None)
    assert settings.db_pool_size == 7
    assert settings.storage_root == project


def test_keys_are_case_insensitive(project, monkeypatch):
    (project / "config.dev.yml").write_text(
        "DATABASE_URL: postgresql+psycopg://u:p@db:5432/batch\n"
        f"STORAGE_ROOT: {project}\n"
        "DB_POOL_SIZE: 9\n"
    )
    monkeypatch.setenv("APP_ENV", "dev")
    assert Settings(_env_file=None).db_pool_size == 9


def test_environment_overrides_the_file(project, monkeypatch):
    write_config(project, "prd")
    monkeypatch.setenv("APP_ENV", "prd")
    monkeypatch.setenv("DB_POOL_SIZE", "2")
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


def sha(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def test_auth_clients_come_from_yaml(project, monkeypatch):
    write_config(project, "stg")
    (project / "config.yml").write_text(
        "AUTH_CLIENTS:\n"
        "  - name: pr-agentic-service\n"
        f'    key_sha256: ["{sha("old")}", "{sha("new")}"]\n'
        "    tenant: pr-agentic-service\n"
        "    relay: true\n"
        "    allowed_tenants: [pr-agentic-service, agent3]\n"
        "    admin: true\n"
        "BATCH_RUNTIMES:\n"
        "  - {type: JUPYTER, profile: python3}\n"
    )
    monkeypatch.setenv("APP_ENV", "stg")
    settings = Settings(_env_file=None)
    assert settings.auth_mode == "api_key"
    # Both hashes work while a key is replaced.
    for key in ("old", "new"):
        client = settings.match_client(key)
        assert client is not None and client.name == "pr-agentic-service"
    assert settings.match_client("other") is None
    assert settings.batch_runtimes[0].profile == "python3"


@pytest.mark.parametrize(
    ("app_env", "allowed"),
    [(None, True), ("dev", True), ("stg", False), ("prd", False)],
)
def test_header_mode_is_local_or_dev_only(
    project, monkeypatch, app_env, allowed
):
    write_config(project, app_env or "dev")
    if app_env:
        monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("AUTH_MODE", "header")
    kwargs: dict[str, Any] = (
        {}
        if app_env
        else {
            "database_url": "postgresql+psycopg://u:p@db:5432/batch",
            "storage_root": project,
        }
    )
    if allowed:
        assert Settings(_env_file=None, **kwargs).auth_mode == "header"
    else:
        with pytest.raises(ValidationError, match="auth_mode header"):
            Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize(
    ("clients", "message"),
    [
        (
            [
                {"name": "aaa", "key_sha256": ["0" * 64], "tenant": "aaa"},
                {"name": "bbb", "key_sha256": ["0" * 64], "tenant": "bbb"},
            ],
            "listed twice",
        ),
        (
            [
                {
                    "name": "aaa",
                    "key_sha256": ["0" * 64],
                    "tenant": "aaa",
                    "allowed_tenants": ["bbb"],
                }
            ],
            "needs relay",
        ),
        (
            [{"name": "aaa", "key_sha256": ["not-hex"], "tenant": "aaa"}],
            "key_sha256",
        ),
    ],
)
def test_auth_clients_are_checked(project, clients, message):
    with pytest.raises(ValidationError, match=message):
        Settings(
            _env_file=None,
            database_url="postgresql+psycopg://u:p@db:5432/batch",
            storage_root=project,
            auth_clients=clients,
        )
