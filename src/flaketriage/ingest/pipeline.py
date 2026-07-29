"""Ingest orchestration: parse, resolve identity, persist.

The whole layer is one function so that the ordering guarantee is visible in one
place: identities are resolved and persisted before executions reference them,
and parse warnings are attributed to the run they came from rather than lost to
the log stream.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from flaketriage.identity.fingerprint import identity_for
from flaketriage.ingest.junit import parse_files
from flaketriage.models import DiffSummary, ParseWarning, RunMetadata
from flaketriage.obs import get_logger
from flaketriage.store.repositories import IngestSummary, RunStore

log = get_logger(__name__)

_XML_GLOB: Final = "*.xml"


def expand_result_paths(patterns: Iterable[str | Path]) -> list[Path]:
    """Resolve files, directories and globs into a sorted list of XML files.

    Globs are expanded here rather than left to the shell: PowerShell and cmd do
    not expand them, so ``--results ./reports/*.xml`` would otherwise work on
    Linux and silently match nothing on Windows.
    """
    found: set[Path] = set()

    for pattern in patterns:
        text = str(pattern)
        candidate = Path(text)

        if candidate.is_dir():
            found.update(p for p in candidate.rglob(_XML_GLOB) if p.is_file())
            continue
        if candidate.is_file():
            found.add(candidate)
            continue

        # Unmatched literal path or a glob: let pathlib decide which.
        anchor = candidate.parent if candidate.parent != Path() else Path()
        pattern_tail = candidate.name
        base = anchor if str(anchor) else Path()
        try:
            found.update(p for p in base.glob(pattern_tail) if p.is_file())
        except (NotImplementedError, ValueError):
            log.warning("results_pattern_invalid", pattern=text)

    return sorted(found)


def ingest(
    store: RunStore,
    metadata: RunMetadata,
    result_paths: Sequence[Path],
    diff: DiffSummary | None = None,
    extra_warnings: Sequence[ParseWarning] = (),
) -> IngestSummary:
    """Parse result files and persist them against ``metadata``."""
    parsed = parse_files(list(result_paths))

    run_pk = store.record_run(metadata)

    new_identities = 0
    rows = []
    for case in parsed.cases:
        identity = identity_for(case)
        identity_id, created = store.upsert_identity(identity)
        new_identities += int(created)
        rows.append((identity_id, case))

    inserted, skipped = store.record_executions(run_pk, rows)

    diff_files = store.record_diff(run_pk, diff) if diff is not None else 0

    warnings = (*parsed.warnings, *extra_warnings)
    if warnings:
        store.record_warnings(run_pk, warnings)

    log.info(
        "ingest_complete",
        run_pk=run_pk,
        run_id=metadata.run_id,
        attempt=metadata.attempt,
        commit_sha=metadata.commit_sha,
        files_parsed=parsed.files_parsed,
        files_rejected=parsed.files_rejected,
        cases_ingested=inserted,
        cases_skipped_duplicate=skipped,
        new_identities=new_identities,
        diff_files=diff_files,
        warnings=len(warnings),
    )

    return IngestSummary(
        run_pk=run_pk,
        cases_ingested=inserted,
        cases_skipped_duplicate=skipped,
        new_identities=new_identities,
        diff_files=diff_files,
        warnings=warnings,
    )
