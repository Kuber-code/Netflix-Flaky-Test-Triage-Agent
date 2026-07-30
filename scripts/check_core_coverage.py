"""Enforce the per-package coverage floor on the deterministic core.

The specification requires at least 80% coverage on ``detect/``, ``identity/`` and
``policy/`` -- the packages that decide things without a model. That number was
true when it was checked by hand, which is not the same as being enforced: a
criterion nothing fails on is a claim, and this project's whole argument is that
claims should be measured.

A **global** ``--cov-fail-under`` would not do the job. The overall figure is
dominated by the larger packages, so the deterministic core could rot to 50% while
the total stayed comfortably above any global threshold. The floor has to be
per-package to mean what the specification says it means.

Run via ``make cov``. Exits non-zero with the offending packages named.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Final

#: Packages that must meet the floor, and the floor itself. These are the layers
#: that produce a verdict or a decision with no model involved, which is why they
#: are held to a standard the classifier is not: a bug here is wrong output, while
#: a bug in the classifier is a worse suggestion.
CORE_PACKAGES: Final[dict[str, float]] = {
    "detect": 0.80,
    "identity": 0.80,
    "policy": 0.80,
}

COVERAGE_XML: Final = Path("coverage.xml")


def main() -> int:
    if not COVERAGE_XML.is_file():
        print(
            f"{COVERAGE_XML} not found; run `make cov` rather than this script alone.",
            file=sys.stderr,
        )
        return 2

    root = ET.parse(COVERAGE_XML).getroot()  # noqa: S314 -- our own build artifact
    rates = {
        package.get("name", ""): float(package.get("line-rate", "0"))
        for package in root.findall(".//package")
    }

    failures: list[str] = []
    missing: list[str] = []

    for name, floor in sorted(CORE_PACKAGES.items()):
        if name not in rates:
            # A package that vanished from the report is a failure, not a pass.
            # Silence here would let a renamed package skip the gate entirely.
            missing.append(name)
            continue
        rate = rates[name]
        status = "ok " if rate >= floor else "FAIL"
        print(f"{status} {name:<12} {rate:6.1%}  (floor {floor:.0%})")
        if rate < floor:
            failures.append(f"{name} at {rate:.1%}, below {floor:.0%}")

    for name in missing:
        print(f"FAIL {name:<12}  absent from coverage.xml")
        failures.append(f"{name} is absent from the coverage report")

    if failures:
        print("\nThe deterministic core is below its coverage floor:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print("\nDeterministic core meets its coverage floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
