"""HCP's init_logger when installed, otherwise the standard logger.yml."""

import logging
import sys
import types
from pathlib import Path

from app.core import logging_setup


def test_falls_back_to_logger_yml(tmp_path: Path, monkeypatch):
    monkeypatch.setitem(sys.modules, "hcp.framework.logger", None)
    path = tmp_path / "logger.yml"
    path.write_text(
        "version: 1\nloggers:\n  app.test_fallback:\n    level: DEBUG\n"
    )
    logging_setup.configure_logging(path)
    assert logging.getLogger("app.test_fallback").level == logging.DEBUG


def test_uses_hcp_when_installed(tmp_path: Path, monkeypatch):
    calls = []
    module = types.ModuleType("hcp.framework.logger")
    module.__dict__["init_logger"] = lambda: calls.append(True)
    monkeypatch.setitem(sys.modules, "hcp.framework.logger", module)
    logging_setup.configure_logging(tmp_path / "unused.yml")
    assert calls == [True]


def test_probe_access_lines_and_http_client_info_are_dropped(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setitem(sys.modules, "hcp.framework.logger", None)
    logging_setup.configure_logging(tmp_path / "missing.yml")
    assert logging.getLogger("httpx2").getEffectiveLevel() == logging.WARNING
    access = logging.getLogger("uvicorn.access")

    def line(path):
        return logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("10.0.0.1:1", "GET", path, "1.1", 200),
            None,
        )

    assert not access.filter(line("/healthz"))
    assert not access.filter(line("/readyz"))
    assert access.filter(line("/api/v1/batches?limit=1"))
