"""flaketriage: deterministic flaky-test detection with an LLM-advisory classifier.

The package is layered so that the deterministic core (`ingest`, `store`,
`identity`, `detect`, `policy`, `report`) never imports from `classify`. The LLM
layer is an enhancement, not a dependency -- see ADR-0001.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
