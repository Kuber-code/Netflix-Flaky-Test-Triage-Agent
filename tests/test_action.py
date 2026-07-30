"""Keep action.yml consistent with the CLI it drives.

A GitHub Action fails in somebody else's repository, minutes after a push, with a
log nobody is watching. So the couplings between the YAML and the Python are
asserted here rather than discovered there: the JSON keys the action reads, the
comment marker its upsert depends on, and the CLI flags it passes.

These tests would have caught every way I could break the action by renaming
something in the Python without opening the YAML.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from flaketriage.cli import app
from flaketriage.report import COMMENT_MARKER, render_json

REPO_ROOT = Path(__file__).resolve().parents[1]
ACTION = REPO_ROOT / "action.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"

runner = CliRunner()


@pytest.fixture(scope="module")
def action() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(ACTION.read_text(encoding="utf-8"))
    return loaded


def action_text() -> str:
    return ACTION.read_text(encoding="utf-8")


def workflow(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return loaded


# --- structure -------------------------------------------------------------


def test_the_action_is_a_composite_action(action: dict[str, Any]) -> None:
    assert action["runs"]["using"] == "composite"
    assert action["runs"]["steps"]


def test_every_step_declares_a_shell_or_uses_an_action(action: dict[str, Any]) -> None:
    """A composite step without `shell` is a hard error at run time."""
    for step in action["runs"]["steps"]:
        assert "shell" in step or "uses" in step, step.get("name")


def test_results_is_the_only_required_input(action: dict[str, Any]) -> None:
    """Everything else has a working default, so the action is usable in one line."""
    required = {name for name, spec in action["inputs"].items() if spec.get("required") is True}
    assert required == {"results"}


def test_the_api_key_has_no_default(action: dict[str, Any]) -> None:
    """A missing key is a mode, not an error -- but it must not be faked."""
    assert action["inputs"]["anthropic-api-key"]["default"] == ""


def test_the_attempt_defaults_to_the_real_run_attempt(action: dict[str, Any]) -> None:
    """Reporting every retry as attempt 1 discards the strongest flake signal."""
    assert "github.run_attempt" in str(action["inputs"]["attempt"]["default"])


def test_gating_is_opt_in(action: dict[str, Any]) -> None:
    """This action reports; turning it into a required check is a team's decision."""
    assert action["inputs"]["fail-on-regression"]["default"] == "false"


# --- couplings to the CLI --------------------------------------------------


def test_the_summary_keys_the_action_reads_all_exist() -> None:
    """The action greps four keys out of the JSON report. Renaming one would break
    it silently, in someone else's CI, with nobody watching the log."""
    payload = json.loads(render_json([]))
    text = action_text()

    for key in ("flaky", "regression", "healthy", "total"):
        assert f"summary {key}" in text or f"summary({key}" in text or key in text
        assert key in payload["summary"], f"{key} is read by action.yml but not emitted"


def test_the_upsert_marker_matches_the_renderer() -> None:
    """The action finds its own previous comment by this exact string."""
    assert COMMENT_MARKER in action_text()


def test_the_cli_flags_the_action_passes_all_exist() -> None:
    """Each flag is checked against the real --help output."""
    flags = {
        "ingest": ["--results", "--sha", "--run-id", "--attempt", "--branch", "--shard", "--diff"],
        "triage": [
            "--since",
            "--sha",
            "--no-llm",
            "--budget-usd",
            "--max-tests",
            "--format",
            "--out",
        ],
        "report": ["--since", "--format", "--out"],
    }
    text = action_text()
    for command, expected in flags.items():
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output
        for flag in expected:
            if flag in text:
                assert flag in result.output, f"{command} has no {flag}, but action.yml passes it"


def test_the_action_installs_the_checked_out_copy_by_default() -> None:
    """So the action and the tool it runs are the same revision by construction."""
    assert "github.action_path" in action_text()


# --- workflows -------------------------------------------------------------


def test_the_example_workflow_triages_after_a_failing_suite() -> None:
    """A triage step guarded by success() never runs on the builds that need it."""
    steps = workflow("example-triage.yml")["jobs"]["test-and-triage"]["steps"]
    run_steps = [step for step in steps if "run" in step]
    suite = next(step for step in run_steps if "pytest" in step["run"])
    assert "|| true" in suite["run"], "the suite must be allowed to fail"


def test_the_example_workflow_requests_least_privilege() -> None:
    permissions = workflow("example-triage.yml")["permissions"]
    assert permissions == {"contents": "read", "pull-requests": "write"}


def test_the_example_workflow_persists_the_run_store() -> None:
    """Every signal is computed from history, so the store must outlive the job."""
    steps = workflow("example-triage.yml")["jobs"]["test-and-triage"]["steps"]
    assert any("actions/cache" in str(step.get("uses", "")) for step in steps)


def test_the_self_triage_workflow_runs_the_local_action() -> None:
    """P8's exit criterion: an action that has never executed is a YAML file."""
    steps = workflow("self-triage.yml")["jobs"]["triage-own-tests"]["steps"]
    assert any(step.get("uses") == "./" for step in steps)


def test_the_self_triage_workflow_needs_no_secret() -> None:
    """It must run on forks, so it uses the deterministic path only."""
    text = (WORKFLOWS / "self-triage.yml").read_text(encoding="utf-8")
    assert "secrets." not in text
    assert 'no-llm: "true"' in text


def test_the_self_triage_workflow_exercises_two_attempts() -> None:
    """A pipeline that never retries can only produce the weaker signal."""
    text = (WORKFLOWS / "self-triage.yml").read_text(encoding="utf-8")
    assert 'attempt: "1"' in text
    assert 'attempt: "2"' in text
