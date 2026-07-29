"""Lookback window parsing for ``--since``."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Final

_DURATION: Final = re.compile(r"^\s*(?P<amount>\d+)\s*(?P<unit>[smhdw])\s*$", re.IGNORECASE)

_UNITS: Final = {
    "s": "seconds",
    "m": "minutes",
    "h": "hours",
    "d": "days",
    "w": "weeks",
}


class InvalidWindowError(ValueError):
    """The ``--since`` value could not be understood."""


def parse_duration(value: str) -> timedelta:
    """Parse ``30d``, ``12h``, ``90m`` into a duration."""
    match = _DURATION.match(value)
    if match is None:
        raise InvalidWindowError(
            f"cannot parse {value!r}; expected a number followed by s, m, h, d or w (e.g. 30d)"
        )
    unit = _UNITS[match.group("unit").lower()]
    return timedelta(**{unit: int(match.group("amount"))})


def cutoff_iso(value: str, *, now: datetime | None = None) -> str:
    """The ISO-8601 timestamp ``value`` corresponds to.

    Returned as a string because the store keeps timestamps as ISO-8601 UTC text
    and comparing in that form keeps the filter a plain lexicographic comparison,
    which is both correct for this format and index-friendly.
    """
    reference = now or datetime.now(UTC)
    return (reference - parse_duration(value)).astimezone(UTC).isoformat()
