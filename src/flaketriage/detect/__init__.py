"""Deterministic detector: flake signals, flake rate, regression path.

This package imports nothing from :mod:`flaketriage.classify` and never will.
That direction of dependency is the layer boundary rule from §5 -- the detector
must be fully functional with the LLM layer disabled, and the import graph is
what makes that checkable rather than merely claimed.
"""

from flaketriage.detect.detector import (
    detect_all,
    detect_for_history,
    find_branch_independent,
    find_historical_instability,
    find_regression,
    find_same_sha_divergence,
)
from flaketriage.detect.footprint import extract_paths, footprint, is_project_frame
from flaketriage.detect.history import History, ShaWindow, WindowStatus, build_history
from flaketriage.detect.infra import is_infra_failure
from flaketriage.detect.models import (
    Confidence,
    Detection,
    FlakeSignal,
    SignalEvidence,
    Verdict,
)
from flaketriage.detect.rates import divergence_rate, ewma, flake_rate, intermittency_rate

__all__ = [
    "Confidence",
    "Detection",
    "FlakeSignal",
    "History",
    "ShaWindow",
    "SignalEvidence",
    "Verdict",
    "WindowStatus",
    "build_history",
    "detect_all",
    "detect_for_history",
    "divergence_rate",
    "ewma",
    "extract_paths",
    "find_branch_independent",
    "find_historical_instability",
    "find_regression",
    "find_same_sha_divergence",
    "flake_rate",
    "footprint",
    "intermittency_rate",
    "is_infra_failure",
    "is_project_frame",
]
