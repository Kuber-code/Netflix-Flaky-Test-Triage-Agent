from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from flaketriage.obs import logging as obs_logging

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _quiet_logging() -> Iterator[None]:
    """Keep test output clean and let each test configure logging afresh."""
    obs_logging.reset_for_testing()
    obs_logging.configure_logging("error")
    yield
    obs_logging.reset_for_testing()


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the suite never makes a real API call.

    Without this, running the tests on a developer machine that happens to export
    ANTHROPIC_API_KEY would silently spend money and make results depend on a
    network. Every LLM behaviour is tested against a scripted client instead; the
    tests that need a key ask for it explicitly.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
