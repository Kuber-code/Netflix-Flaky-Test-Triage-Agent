from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flaketriage.cli import app
from flaketriage.store.repositories import RunStore

FIXTURES = Path(__file__).parent / "fixtures" / "junit"
runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    """A directory with its own flaketriage.toml, so the store lands in tmp."""
    (tmp_path / "flaketriage.toml").write_text(
        '[store]\npath = "state/store.sqlite"\n', encoding="utf-8"
    )
    yield tmp_path


def invoke(workspace: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--config", str(workspace / "flaketriage.toml"), *args])
    return result.exit_code, result.output


def test_ingest_writes_to_the_configured_store(workspace: Path) -> None:
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(FIXTURES / "pytest.xml"),
        "--sha",
        "abc123def456789",
        "--run-id",
        "run-42",
    )
    assert code == 0, output
    assert "executions recorded" in output

    store_path = workspace / "state" / "store.sqlite"
    assert store_path.is_file()
    with RunStore.open(store_path) as store:
        assert store.count_executions() == 5
        assert store.count_identities() == 5


def test_ingest_accepts_repeated_results_options(workspace: Path) -> None:
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(FIXTURES / "pytest.xml"),
        "--results",
        str(FIXTURES / "surefire.xml"),
        "--sha",
        "abc123",
        "--run-id",
        "run-1",
    )
    assert code == 0, output
    with RunStore.open(workspace / "state" / "store.sqlite") as store:
        assert store.count_executions() == 9


def test_ingest_records_the_attempt_number(workspace: Path) -> None:
    for attempt in ("1", "2"):
        code, output = invoke(
            workspace,
            "ingest",
            "--results",
            str(FIXTURES / "pytest.xml"),
            "--sha",
            "abc123",
            "--run-id",
            "run-1",
            "--attempt",
            attempt,
        )
        assert code == 0, output

    with RunStore.open(workspace / "state" / "store.sqlite") as store:
        attempts = store.connection.execute("SELECT attempt FROM runs ORDER BY attempt").fetchall()
        assert [row["attempt"] for row in attempts] == [1, 2]
        assert store.count_executions() == 10


def test_ingest_fails_loudly_when_nothing_matches(workspace: Path) -> None:
    """Silently ingesting zero files would make a broken CI step look healthy."""
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(workspace / "no-such-*.xml"),
        "--sha",
        "abc123",
        "--run-id",
        "run-1",
    )
    assert code == 1
    assert "No result files matched" in output


def test_ingest_surfaces_parse_warnings(workspace: Path) -> None:
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(FIXTURES / "truncated.xml"),
        "--sha",
        "abc123",
        "--run-id",
        "run-1",
    )
    assert code == 0, output
    assert "malformed_xml" in output
    assert "parse warnings" in output


def test_ingest_requires_sha_and_run_id(workspace: Path) -> None:
    code, _ = invoke(workspace, "ingest", "--results", str(FIXTURES / "pytest.xml"))
    assert code == 2


def test_ingest_rejects_a_zero_attempt(workspace: Path) -> None:
    code, _ = invoke(
        workspace,
        "ingest",
        "--results",
        str(FIXTURES / "pytest.xml"),
        "--sha",
        "abc",
        "--run-id",
        "r",
        "--attempt",
        "0",
    )
    assert code == 2


def test_ingest_with_a_diff_file(workspace: Path) -> None:
    patch = workspace / "change.patch"
    patch.write_text(
        "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n",
        encoding="utf-8",
    )
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(FIXTURES / "pytest.xml"),
        "--sha",
        "abc123",
        "--run-id",
        "run-1",
        "--diff",
        str(patch),
    )
    assert code == 0, output
    with RunStore.open(workspace / "state" / "store.sqlite") as store:
        assert store.diff_paths_for_sha("abc123") == frozenset({"src/a.py"})
