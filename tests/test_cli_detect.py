from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from flaketriage.cli import app

runner = CliRunner()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[Path]:
    (tmp_path / "flaketriage.toml").write_text(
        '[store]\npath = "state/store.sqlite"\n', encoding="utf-8"
    )
    yield tmp_path


def junit(name: str, failing: bool) -> str:
    body = (
        '<failure message="AssertionError: flaked" type="AssertionError">'
        "tests/test_auth.py:27: AssertionError</failure>"
        if failing
        else ""
    )
    return (
        '<testsuite name="suite" file="tests/test_auth.py">'
        f'<testcase name="{name}" classname="tests.test_auth" file="tests/test_auth.py">'
        f"{body}</testcase></testsuite>"
    )


def invoke(workspace: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(app, ["--config", str(workspace / "flaketriage.toml"), *args])
    return result.exit_code, result.output


def seed_flaky(workspace: Path) -> None:
    """One commit, two attempts, disagreeing outcomes: a confirmed flake."""
    for attempt, failing in ((1, False), (2, True)):
        path = workspace / f"results-{attempt}.xml"
        path.write_text(junit("test_login", failing), encoding="utf-8")
        code, output = invoke(
            workspace,
            "ingest",
            "--results",
            str(path),
            "--sha",
            "abc123def456",
            "--run-id",
            "run-1",
            "--attempt",
            str(attempt),
        )
        assert code == 0, output


def test_detect_reports_a_flake(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "detect")
    assert code == 0, output
    assert "flaky" in output
    assert "test_login" in output


def test_detect_json_is_machine_readable(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "detect", "--json")
    assert code == 0, output
    payload = json.loads(output)
    assert payload["summary"]["flaky"] == 1
    assert payload["llm_enabled"] is False


def test_detect_without_a_store_fails_with_a_useful_message(workspace: Path) -> None:
    """Silently reporting "no flakes" when there is no data would be a lie."""
    code, output = invoke(workspace, "detect")
    assert code == 1
    assert "No run store" in output
    assert "ingest" in output


def test_detect_rejects_an_unparseable_since_window(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "detect", "--since", "last tuesday")
    assert code == 1
    assert "cannot parse" in output


def test_detect_accepts_valid_since_windows(workspace: Path) -> None:
    seed_flaky(workspace)
    for window in ("30d", "12h", "90m", "2w", "3600s"):
        code, output = invoke(workspace, "detect", "--since", window)
        assert code == 0, f"{window}: {output}"


def test_a_narrow_window_finds_nothing(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "detect", "--since", "60s")
    assert code == 0, output


def test_report_markdown_is_emitted_verbatim(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "report", "--format", "markdown")
    assert code == 0, output
    assert output.startswith("### Flaky test triage")
    assert "| verdict | confidence | flake rate | test |" in output


def test_report_rejects_an_unknown_format(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "report", "--format", "yaml")
    assert code == 1
    assert "Unknown format" in output


def test_triage_no_llm_produces_a_report_with_zero_api_calls(workspace: Path) -> None:
    """Acceptance criterion: the deterministic path is complete on its own."""
    seed_flaky(workspace)
    code, output = invoke(workspace, "triage", "--no-llm")
    assert code == 0, output
    assert "flaky" in output
    assert "test_login" in output
    # --no-llm must not print the "classifier not built" note.
    assert "classifier" not in output


def test_triage_without_a_key_says_so_and_still_reports(workspace: Path) -> None:
    """A missing key is a mode, not an error -- but it must be visible.

    Otherwise the report looks like a full triage that happened to find no causes.
    """
    code, output = invoke(workspace, "triage")
    assert code == 1  # no store yet
    seed_flaky(workspace)

    code, output = invoke(workspace, "triage")
    assert code == 0, output
    assert "no ANTHROPIC_API_KEY" in output
    assert "test_login" in output  # the deterministic report is still produced


def test_triage_can_be_scoped_to_a_commit(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "triage", "--no-llm", "--sha", "abc123def456")
    assert code == 0, output
    assert "test_login" in output


def test_triage_scoped_to_a_clean_commit_says_so(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(workspace, "triage", "--no-llm", "--sha", "0000000000")
    assert code == 0, output
    assert "No failures recorded" in output


def test_triage_json_and_markdown_formats(workspace: Path) -> None:
    seed_flaky(workspace)
    for fmt in ("json", "markdown", "terminal"):
        code, output = invoke(workspace, "triage", "--no-llm", "--format", fmt)
        assert code == 0, output
        assert output.strip()


def test_started_at_accepts_iso8601_with_an_offset(workspace: Path) -> None:
    """Every CI system hands you an offset-bearing timestamp."""
    path = workspace / "r.xml"
    path.write_text(junit("test_login", False), encoding="utf-8")
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(path),
        "--sha",
        "abc",
        "--run-id",
        "r",
        "--started-at",
        "2026-07-15T09:00:00+00:00",
    )
    assert code == 0, output


def test_started_at_accepts_a_naive_timestamp_as_utc(workspace: Path) -> None:
    path = workspace / "r.xml"
    path.write_text(junit("test_login", False), encoding="utf-8")
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(path),
        "--sha",
        "abc",
        "--run-id",
        "r",
        "--started-at",
        "2026-07-15T09:00:00",
    )
    assert code == 0, output


def test_started_at_rejects_nonsense_with_a_useful_message(workspace: Path) -> None:
    path = workspace / "r.xml"
    path.write_text(junit("test_login", False), encoding="utf-8")
    code, output = invoke(
        workspace,
        "ingest",
        "--results",
        str(path),
        "--sha",
        "abc",
        "--run-id",
        "r",
        "--started-at",
        "last tuesday",
    )
    assert code == 1
    assert "Cannot parse --started-at" in output


def test_out_writes_utf8_markdown_to_a_file(workspace: Path) -> None:
    """The Action path writes a file; it must not depend on the console codepage."""
    seed_flaky(workspace)
    target = workspace / "nested" / "comment.md"
    code, output = invoke(workspace, "report", "--format", "markdown", "--out", str(target))
    assert code == 0, output
    text = target.read_text(encoding="utf-8")
    assert text.startswith("### Flaky test triage")
    assert text.isascii(), "PR comment markdown must be ASCII-only"


def test_out_writes_json_to_a_file(workspace: Path) -> None:
    seed_flaky(workspace)
    target = workspace / "detections.json"
    code, output = invoke(workspace, "report", "--format", "json", "--out", str(target))
    assert code == 0, output
    assert json.loads(target.read_text(encoding="utf-8"))["summary"]["flaky"] == 1


def test_out_is_rejected_for_terminal_format(workspace: Path) -> None:
    seed_flaky(workspace)
    code, output = invoke(
        workspace, "report", "--format", "terminal", "--out", str(workspace / "x.txt")
    )
    assert code == 1
    assert "only meaningful with" in output
