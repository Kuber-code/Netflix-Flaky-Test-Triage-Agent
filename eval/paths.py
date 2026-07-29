"""Shared filesystem locations for the evaluation harness.

Kept in one place so the corpus generator, the baseline, and the runner cannot
drift apart on where the dataset lives.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

EVAL_DIR: Final = Path(__file__).resolve().parent
REPO_ROOT: Final = EVAL_DIR.parent

DATASET_DIR: Final = EVAL_DIR / "dataset"
RESULTS_DIR: Final = EVAL_DIR / "results"
LATEST_RESULTS: Final = RESULTS_DIR / "latest.md"


def ensure_dirs() -> None:
    """Create the dataset and results directories if they do not exist."""
    DATASET_DIR.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
