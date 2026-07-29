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


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT
