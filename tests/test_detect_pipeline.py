"""End-to-end detection over a real store, with zero model calls.

This is the phase P3 exit criterion: ingest JUnit XML, detect, render -- all of
it working with the LLM layer absent. The import-graph test at the bottom is what
keeps that property from quietly eroding.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from flaketriage.detect import Verdict, detect_all
from flaketriage.ingest.pipeline import ingest
from flaketriage.models import Outcome, RunMetadata
from flaketriage.report import render_json, render_markdown
from flaketriage.store.db import IN_MEMORY
from flaketriage.store.repositories import RunStore

BASE_TIME = datetime(2026, 7, 20, 10, 0, tzinfo=UTC)


@pytest.fixture
def store() -> Iterator[RunStore]:
    with RunStore.open(IN_MEMORY) as opened:
        yield opened


def junit(cases: list[tuple[str, Outcome]], *, suite_file: str = "tests/test_auth.py") -> bytes:
    """Minimal JUnit XML for the given (name, outcome) pairs."""
    parts = [f'<testsuite name="suite" file="{suite_file}">']
    for name, outcome in cases:
        parts.append(f'<testcase name="{name}" classname="tests.test_auth" file="{suite_file}">')
        if outcome is Outcome.FAIL:
            parts.append(
                '<failure message="AssertionError: expected 200, got 503" type="AssertionError">'
                f"{suite_file}:27: AssertionError</failure>"
            )
        elif outcome is Outcome.ERROR:
            parts.append(
                '<error message="ConnectionResetError" type="ConnectionResetError">'
                f"{suite_file}:31: ConnectionResetError</error>"
            )
        elif outcome is Outcome.SKIP:
            parts.append("<skipped/>")
        parts.append("</testcase>")
    parts.append("</testsuite>")
    return "".join(parts).encode()


def ingest_run(
    store: RunStore,
    tmp_path: Path,
    index: int,
    cases: list[tuple[str, Outcome]],
    *,
    sha: str | None = None,
    attempt: int = 1,
    branch: str = "main",
) -> None:
    path = tmp_path / f"results-{index}-{attempt}.xml"
    path.write_bytes(junit(cases))
    ingest(
        store,
        RunMetadata(
            commit_sha=sha or f"sha{index:04d}",
            run_id=f"run-{index}",
            attempt=attempt,
            branch=branch,
            started_at=BASE_TIME + timedelta(minutes=index),
        ),
        [path],
    )


def test_full_pipeline_from_xml_to_verdicts(store: RunStore, tmp_path: Path) -> None:
    # A stable test, a test that diverges at one commit, and a clean regression.
    for index in range(4):
        ingest_run(
            store,
            tmp_path,
            index,
            [
                ("test_stable", Outcome.PASS),
                ("test_flaky", Outcome.PASS),
                ("test_regressing", Outcome.PASS),
            ],
        )

    # Retry of run 4: same commit, test_flaky fails this time.
    ingest_run(
        store,
        tmp_path,
        4,
        [
            ("test_stable", Outcome.PASS),
            ("test_flaky", Outcome.PASS),
            ("test_regressing", Outcome.PASS),
        ],
        sha="pivot",
    )
    ingest_run(
        store,
        tmp_path,
        4,
        [
            ("test_stable", Outcome.PASS),
            ("test_flaky", Outcome.FAIL),
            ("test_regressing", Outcome.FAIL),
        ],
        sha="pivot",
        attempt=2,
    )
    ingest_run(
        store,
        tmp_path,
        5,
        [
            ("test_stable", Outcome.PASS),
            ("test_flaky", Outcome.PASS),
            ("test_regressing", Outcome.FAIL),
        ],
        sha="after",
    )

    detections = {d.identity.test_name: d for d in detect_all(store)}

    assert detections["test_stable"].verdict is Verdict.HEALTHY
    assert detections["test_flaky"].verdict is Verdict.FLAKY
    assert detections["test_flaky"].retry_data_available is True
    # The regressing test also has a divergent commit at the pivot (the retry
    # passed at attempt 1 and failed at attempt 2), so divergence wins -- which is
    # correct: a commit that produced both outcomes is non-deterministic.
    assert detections["test_regressing"].verdict is Verdict.FLAKY


def test_a_clean_regression_is_detected_end_to_end(store: RunStore, tmp_path: Path) -> None:
    for index in range(4):
        ingest_run(store, tmp_path, index, [("test_orders", Outcome.PASS)])
    for index in range(4, 7):
        ingest_run(store, tmp_path, index, [("test_orders", Outcome.FAIL)])

    (detection,) = detect_all(store)
    assert detection.verdict is Verdict.REGRESSION
    assert detection.regression_sha == "sha0004"
    assert detection.needs_classification is False


def test_infra_errors_do_not_make_a_test_look_flaky(store: RunStore, tmp_path: Path) -> None:
    """A runner preemption must never appear in a test's flake rate."""
    for index in range(4):
        ingest_run(store, tmp_path, index, [("test_orders", Outcome.PASS)])

    path = tmp_path / "infra.xml"
    path.write_bytes(
        b'<testsuite name="suite" file="tests/test_auth.py">'
        b'<testcase name="test_orders" classname="tests.test_auth" file="tests/test_auth.py">'
        b'<error message="No space left on device" type="IOError">boom</error>'
        b"</testcase></testsuite>"
    )
    ingest(
        store,
        RunMetadata(
            commit_sha="sha0000",
            run_id="run-0",
            attempt=2,
            started_at=BASE_TIME + timedelta(minutes=30),
        ),
        [path],
    )

    (detection,) = detect_all(store)
    assert detection.infra_excluded == 1
    assert detection.flake_rate == 0.0
    assert detection.verdict is Verdict.HEALTHY


def test_a_renamed_test_is_reported_once_with_merged_history(
    store: RunStore, tmp_path: Path
) -> None:
    for index in range(3):
        ingest_run(store, tmp_path, index, [("test_login_succeeds", Outcome.PASS)])
    ingest_run(store, tmp_path, 3, [("test_signin_succeeds", Outcome.PASS)])

    detections = detect_all(store)
    assert len(detections) == 1
    assert detections[0].identity.test_name == "test_signin_succeeds"
    assert detections[0].observations == 4
    assert detections[0].merged_uncertain is True


def test_the_since_window_excludes_old_executions(store: RunStore, tmp_path: Path) -> None:
    ingest_run(store, tmp_path, 0, [("test_orders", Outcome.FAIL)])
    ingest_run(store, tmp_path, 1, [("test_orders", Outcome.PASS)])

    cutoff = (BASE_TIME + timedelta(minutes=1)).isoformat()
    detections = detect_all(store, since=cutoff)
    assert detections[0].observations == 1


def test_detection_can_be_scoped_to_one_commit(store: RunStore, tmp_path: Path) -> None:
    ingest_run(store, tmp_path, 0, [("test_a", Outcome.PASS), ("test_b", Outcome.FAIL)])

    failing = store.failing_identities_at_sha("sha0000")
    detections = detect_all(store, identity_ids=failing)
    assert [detection.identity.test_name for detection in detections] == ["test_b"]


def test_an_empty_store_yields_no_detections(store: RunStore) -> None:
    assert detect_all(store) == []


# --- rendering -------------------------------------------------------------


def test_json_output_carries_the_numbers_not_just_the_verdict(
    store: RunStore, tmp_path: Path
) -> None:
    import json

    ingest_run(store, tmp_path, 0, [("test_flaky", Outcome.PASS)], sha="x")
    ingest_run(store, tmp_path, 1, [("test_flaky", Outcome.FAIL)], sha="x", attempt=2)

    payload = json.loads(render_json(detect_all(store)))
    assert payload["schema_version"] == 1
    assert payload["llm_enabled"] is False
    assert payload["summary"]["flaky"] == 1
    entry = payload["detections"][0]
    assert entry["verdict"] == "flaky"
    assert entry["divergence_rate"] > 0
    assert entry["signals"][0]["signal"] == "same_sha_divergence"
    assert entry["retry_data_available"] is True


def test_markdown_leads_with_the_line_a_reader_needs(store: RunStore, tmp_path: Path) -> None:
    ingest_run(store, tmp_path, 0, [("test_flaky", Outcome.PASS)], sha="x")
    ingest_run(store, tmp_path, 1, [("test_flaky", Outcome.FAIL)], sha="x", attempt=2)

    markdown = render_markdown(detect_all(store))
    assert "appear unrelated to your change" in markdown
    assert "<details>" in markdown
    assert "the cause classifier was not run" in markdown


def test_markdown_says_so_plainly_when_a_regression_is_present(
    store: RunStore, tmp_path: Path
) -> None:
    for index in range(4):
        ingest_run(store, tmp_path, index, [("test_orders", Outcome.PASS)])
    for index in range(4, 7):
        ingest_run(store, tmp_path, index, [("test_orders", Outcome.FAIL)])

    markdown = render_markdown(detect_all(store))
    assert "look like real regressions" in markdown
    assert "not eligible for quarantine" in markdown


def test_markdown_caps_its_table(store: RunStore, tmp_path: Path) -> None:
    """A bot that writes an essay on every failed build gets muted."""
    cases = [(f"test_{index}", Outcome.PASS) for index in range(12)]
    ingest_run(store, tmp_path, 0, cases, sha="x")
    ingest_run(store, tmp_path, 1, [(name, Outcome.FAIL) for name, _ in cases], sha="x", attempt=2)

    markdown = render_markdown(detect_all(store), max_rows=5)
    assert markdown.count("| flaky |") == 5
    assert "7 further finding(s) omitted" in markdown


def test_markdown_when_nothing_was_found(store: RunStore, tmp_path: Path) -> None:
    ingest_run(store, tmp_path, 0, [("test_a", Outcome.PASS)])
    assert "No flakes, regressions or unexplained failures" in render_markdown(detect_all(store))


# --- layer boundary ---------------------------------------------------------


def test_the_deterministic_core_does_not_import_the_classifier() -> None:
    """§5's layer boundary rule, enforced rather than asserted.

    If a future change makes `detect` reach into `classify`, `--no-llm` stops
    being a real guarantee and this test is the thing that notices.
    """
    import ast

    core = ["detect", "ingest", "identity", "store", "policy", "report"]
    root = Path(__file__).resolve().parents[1] / "src" / "flaketriage"

    offenders: list[str] = []
    for package in core:
        for module in (root / package).rglob("*.py"):
            tree = ast.parse(module.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                if any("flaketriage.classify" in name for name in names):
                    offenders.append(f"{package}/{module.name}")

    assert offenders == []
