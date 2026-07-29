"""Command-line interface.

Command surface is fixed by the specification (§6.7) and declared in full here
from phase P0 onward, with unimplemented commands failing explicitly. A stub
that prints nothing and exits 0 is worse than no command at all: it makes a
missing feature look like an empty result.

stdout carries report data; stderr carries logs and diagnostics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table

from flaketriage import __version__
from flaketriage.config import Config, load_config
from flaketriage.ingest import expand_result_paths, parse_diff_file
from flaketriage.ingest import ingest as run_ingest
from flaketriage.models import RunMetadata
from flaketriage.obs import configure_logging
from flaketriage.store import RunStore

app = typer.Typer(
    name="flaketriage",
    help="Deterministic flaky-test detection with an LLM-advisory cause classifier.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)

stdout = Console()
stderr = Console(stderr=True)

_EXIT_NOT_IMPLEMENTED: Final = 2


class AppState:
    """Options resolved by the root callback and shared by all subcommands."""

    def __init__(self) -> None:
        self.config: Config = Config()


state = AppState()


def _not_implemented(command: str, phase: str) -> None:
    stderr.print(
        f"[yellow]{command}[/yellow] is not implemented yet (scheduled for build phase {phase})."
    )
    raise typer.Exit(_EXIT_NOT_IMPLEMENTED)


def _version_callback(value: bool) -> None:
    if value:
        stdout.print(f"flaketriage {__version__}")
        raise typer.Exit(0)


@app.callback()
def main(
    config_path: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Path to flaketriage.toml. Defaults to the nearest one above the cwd.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    log_level: Annotated[
        str, typer.Option("--log-level", help="debug, info, warning, or error.")
    ] = "info",
    human_logs: Annotated[
        bool,
        typer.Option("--human-logs", help="Render logs for humans instead of as JSON."),
    ] = False,
    _version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Resolve global options before dispatching to a subcommand."""
    configure_logging(log_level, json_output=not human_logs)
    state.config = load_config(config_path)


@app.command()
def ingest(
    results: Annotated[
        list[str],
        typer.Option(
            "--results",
            help="JUnit XML files, directories or globs. Repeatable.",
        ),
    ],
    sha: Annotated[str, typer.Option("--sha", help="Commit SHA under test.")],
    run_id: Annotated[str, typer.Option("--run-id", help="CI run identifier.")],
    attempt: Annotated[
        int,
        typer.Option(
            "--attempt",
            min=1,
            help="Retry attempt number. Divergence between attempts at one SHA "
            "is the strongest flake signal, so getting this right matters.",
        ),
    ] = 1,
    branch: Annotated[str | None, typer.Option("--branch", help="Branch under test.")] = None,
    shard: Annotated[
        str | None,
        typer.Option("--shard", help="Shard id; order-dependent flakes cluster by shard."),
    ] = None,
    worker: Annotated[str | None, typer.Option("--worker", help="Worker or runner id.")] = None,
    diff_file: Annotated[
        Path | None,
        typer.Option(
            "--diff",
            help="Unified diff (git diff --unified=0) for this change.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    started_at: Annotated[
        datetime | None,
        typer.Option("--started-at", help="Run start time. Defaults to now, in UTC."),
    ] = None,
) -> None:
    """Parse test results and persist them to the run store.

    Re-ingesting the same run, attempt and shard is a no-op rather than a
    duplicate: CI retries ingest steps, and double-counting observations would
    corrupt every flake rate downstream.
    """
    paths = expand_result_paths(results)
    if not paths:
        stderr.print(
            f"[red]No result files matched[/red] {', '.join(results)}. Nothing was ingested."
        )
        raise typer.Exit(1)

    metadata = RunMetadata(
        commit_sha=sha,
        run_id=run_id,
        attempt=attempt,
        branch=branch,
        shard_id=shard,
        worker_id=worker,
        started_at=_as_utc(started_at),
    )

    diff_result = parse_diff_file(diff_file) if diff_file is not None else None

    with RunStore.open(state.config.store_path()) as store:
        summary = run_ingest(
            store,
            metadata,
            paths,
            diff=diff_result,
            extra_warnings=diff_result.warnings if diff_result is not None else (),
        )

    table = Table(title=f"Ingested {sha[:12]} (run {run_id}, attempt {attempt})", box=None)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("result files", str(len(paths)))
    table.add_row("executions recorded", str(summary.cases_ingested))
    table.add_row("duplicates skipped", str(summary.cases_skipped_duplicate))
    table.add_row("new test identities", str(summary.new_identities))
    table.add_row("diff files", str(summary.diff_files))
    table.add_row("parse warnings", str(len(summary.warnings)))
    stdout.print(table)

    # Warnings are surfaced, not buried: a half-truncated result file means the
    # run's data is incomplete and any flake rate computed from it is suspect.
    for warning in summary.warnings:
        stderr.print(f"[yellow]warning[/yellow] {warning.reason}: {warning.origin}")


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@app.command()
def detect(
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30d.")] = "30d",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout.")] = False,
) -> None:
    """Run the deterministic detector. Never calls a model."""
    del since, json_output
    _not_implemented("detect", "P3")


@app.command()
def triage(
    sha: Annotated[str | None, typer.Option("--sha", help="Commit SHA to triage.")] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Deterministic output only; zero API calls.")
    ] = False,
    budget_usd: Annotated[
        float | None, typer.Option("--budget-usd", help="Per-invocation cost ceiling.")
    ] = None,
    max_tests: Annotated[
        int | None, typer.Option("--max-tests", help="Cap on tests classified.")
    ] = None,
) -> None:
    """Detect flakes and, unless --no-llm is given, classify their likely cause."""
    del sha, no_llm, budget_usd, max_tests
    _not_implemented("triage", "P3/P4")


@app.command()
def report(
    output_format: Annotated[
        str, typer.Option("--format", help="terminal, json, or markdown.")
    ] = "terminal",
) -> None:
    """Render the most recent triage result."""
    del output_format
    _not_implemented("report", "P3/P8")


@app.command()
def policy(
    show_quarantine: Annotated[
        bool, typer.Option("--show-quarantine", help="List quarantined tests.")
    ] = False,
    expiring: Annotated[
        bool, typer.Option("--expiring", help="Only show quarantines nearing TTL expiry.")
    ] = False,
) -> None:
    """Show deterministic quarantine decisions."""
    del show_quarantine, expiring
    _not_implemented("policy", "P7")


# Registered as `eval` per the spec; the Python name avoids shadowing the builtin.
@app.command(name="eval")
def evaluate(
    subset: Annotated[
        str | None, typer.Option("--subset", help="Restrict to one taxonomy class.")
    ] = None,
) -> None:
    """Run the evaluation harness against the labeled corpus."""
    del subset
    _not_implemented("eval", "P5")


@app.command()
def stats() -> None:
    """Show run metrics: causes, abstention rate, cost, cache hit rate, latency."""
    _not_implemented("stats", "P6")


if __name__ == "__main__":  # pragma: no cover
    app()
