"""Test identity: fingerprinting, parameterization, and alias resolution.

Fingerprinting (the stable primary key) lands with ingest in P1 because the
schema needs the ``(suite_path, test_name, parameters)`` columns from the start.
Alias resolution across renames needs cross-run evidence and arrives in P2.
"""

from flaketriage.identity.fingerprint import (
    fingerprint,
    identity_for,
    normalize_suite_path,
    split_parameters,
)

__all__ = ["fingerprint", "identity_for", "normalize_suite_path", "split_parameters"]
