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
