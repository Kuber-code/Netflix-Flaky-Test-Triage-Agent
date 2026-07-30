"""Content-addressed classification cache.

The same stack trace recurs across runs -- that is what makes a flake a flake --
so the same context gets classified over and over. Keying on a hash of the
assembled context makes the repeat free.

The key covers the context, the model, and the prompt version hash. Omitting any
of the three would serve a stale answer after a change that should have altered
it: a prompt edit is exactly the moment you must not be reading yesterday's
outputs, and comparing two models is exactly the moment they must not share
entries.

Stored as one JSON file per key rather than in SQLite: the cache is disposable, a
corrupt entry must cost one re-classification rather than a whole run, and being
able to read and delete a single entry by hand is worth more here than
transactions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Final

from flaketriage.models import Classification, DowngradeReason
from flaketriage.obs import get_logger

log = get_logger(__name__)

CACHE_FORMAT_VERSION: Final = 1
_SHARD_WIDTH: Final = 2


def context_key(context: str, *, model: str, prompt_version: str) -> str:
    """Stable key for one classification request."""
    payload = "\x1f".join((str(CACHE_FORMAT_VERSION), model, prompt_version, context))
    return hashlib.sha256(payload.encode()).hexdigest()


class ClassificationCache:
    """Filesystem cache with hit/miss counters for the metrics table."""

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self._root = root
        self._enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def lookups(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0

    def get(self, key: str) -> Classification | None:
        if not self._enabled:
            return None
        path = self._path(key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            cached = Classification.model_validate(payload)
        except FileNotFoundError:
            self.misses += 1
            return None
        except (OSError, ValueError):
            # A corrupt or outdated entry is a miss, not an error. Removing it
            # keeps one bad write from poisoning every future run.
            log.warning("cache_entry_unreadable", key=key[:12])
            path.unlink(missing_ok=True)
            self.misses += 1
            return None
        self.hits += 1
        return cached

    def put(self, key: str, classification: Classification) -> None:
        """Store a result. A cache that cannot be written is not an error.

        Abstentions caused by transient conditions -- an API error, an exhausted
        budget -- are deliberately not cached: they describe the run, not the
        evidence, and caching them would make one bad afternoon permanent.
        """
        if not self._enabled or not _is_cacheable(classification):
            return
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # Written to a temporary file and moved, so a crash mid-write cannot
            # leave a truncated entry that every later run has to discover.
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                classification.model_dump_json(indent=2), encoding="utf-8", newline="\n"
            )
            temporary.replace(path)
            self.writes += 1
        except OSError as exc:
            log.warning("cache_write_failed", key=key[:12], error=str(exc))

    def _path(self, key: str) -> Path:
        # Sharded by the first bytes of the key: a flat directory of tens of
        # thousands of files is slow to list on every platform that matters.
        return self._root / key[:_SHARD_WIDTH] / f"{key}.json"


def _is_cacheable(classification: Classification) -> bool:
    transient = {
        DowngradeReason.API_ERROR,
        DowngradeReason.BUDGET_EXHAUSTED,
        DowngradeReason.LLM_DISABLED,
    }
    return classification.downgrade_reason not in transient
