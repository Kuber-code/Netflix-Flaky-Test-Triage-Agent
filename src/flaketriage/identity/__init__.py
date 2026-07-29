"""Test identity: fingerprinting, parameterization, and alias resolution.

Fingerprinting (the stable primary key) lands with ingest in P1 because the
schema needs the ``(suite_path, test_name, parameters)`` columns from the start.
Alias resolution across renames needs cross-run evidence and arrives in P2.
"""

from flaketriage.identity.alias import AliasCandidate, combined_distance, detect_renames
from flaketriage.identity.fingerprint import (
    fingerprint,
    identity_for,
    normalize_suite_path,
    split_parameters,
)
from flaketriage.identity.reconcile import ReconcileResult, reconcile_renames
from flaketriage.identity.similarity import edit_distance, normalized_distance

__all__ = [
    "AliasCandidate",
    "ReconcileResult",
    "combined_distance",
    "detect_renames",
    "edit_distance",
    "fingerprint",
    "identity_for",
    "normalize_suite_path",
    "normalized_distance",
    "reconcile_renames",
    "split_parameters",
]
