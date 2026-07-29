from __future__ import annotations

import json

import pytest

from flaketriage.obs import logging as obs_logging


def test_logs_are_json_on_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    """stdout is reserved for report data; logs must not pollute it."""
    obs_logging.reset_for_testing()
    obs_logging.configure_logging("info", json_output=True)
    obs_logging.get_logger("test").info("ingest_started", files=3)

    captured = capsys.readouterr()
    assert captured.out == ""
    payload = json.loads(captured.err.strip().splitlines()[-1])
    assert payload["event"] == "ingest_started"
    assert payload["files"] == 3
    assert payload["level"] == "info"
    assert "timestamp" in payload


def test_level_filtering_applies() -> None:
    obs_logging.reset_for_testing()
    obs_logging.configure_logging("error")
    logger = obs_logging.get_logger("test")
    assert logger.debug("suppressed") is None


def test_configure_is_idempotent() -> None:
    obs_logging.reset_for_testing()
    obs_logging.configure_logging("info")
    obs_logging.configure_logging("debug")  # ignored: already configured
    assert obs_logging._state["configured"] is True
