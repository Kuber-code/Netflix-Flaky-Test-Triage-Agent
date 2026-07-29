"""Command-line interface.

Command surface is fixed by the specification (§6.7) and declared in full here
from phase P0 onward, with unimplemented commands failing explicitly. A stub
that prints nothing and exits 0 is worse than no command at all: it makes a
missing feature look like an empty result.

stdout carries report data; stderr carries logs and diagnostics.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table

from flaketriage import __version__
from flaketriage.config import Config, load_config
from flaketriage.detect import Detection, detect_all
from flaketriage.ingest import expand_result_paths, parse_diff_file
from flaketriage.ingest import ingest as run_ingest
from flaketriage.models import RunMetadata
from flaketriage.obs import configure_logging
from flaketriage.report import render_json, render_markdown, render_terminal
from flaketriage.report.window import InvalidWindowError, cutoff_iso
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
        str | None,
        typer.Option(
            "--started-at",
            help="Run start time as ISO-8601, e.g. 2026-07-15T09:00:00+00:00. Defaults to now.",
        ),
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
            identity_config=state.config.identity,
        )

    table = Table(title=f"Ingested {sha[:12]} (run {run_id}, attempt {attempt})", box=None)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("result files", str(len(paths)))
    table.add_row("executions recorded", str(summary.cases_ingested))
    table.add_row("duplicates skipped", str(summary.cases_skipped_duplicate))
    table.add_row("new test identities", str(summary.new_identities))
    table.add_row("renames merged", str(summary.aliases_recorded))
    table.add_row("diff files", str(summary.diff_files))
    table.add_row("parse warnings", str(len(summary.warnings)))
    stdout.print(table)

    # An inferred merge is reported, never applied silently: a wrongly merged
    # history produces a flake rate that describes no real test.
    if summary.uncertain_aliases:
        stderr.print(
            f"[yellow]merged_uncertain[/yellow] {summary.uncertain_aliases} rename(s) were "
            "inferred from name similarity rather than observed."
        )

    # Warnings are surfaced, not buried: a half-truncated result file means the
    # run's data is incomplete and any flake rate computed from it is suspect.
    for warning in summary.warnings:
        stderr.print(f"[yellow]warning[/yellow] {warning.reason}: {warning.origin}")


def _as_utc(value: str | None) -> datetime:
    """Parse an ISO-8601 timestamp, assuming UTC when no offset is given.

    Parsed here rather than by Typer's datetime converter, which accepts a fixed
    list of formats that excludes UTC offsets -- and an offset-bearing ISO-8601
    string is exactly what every CI system hands you.
    """
    if value is None:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        stderr.print(
            f"[red]Cannot parse --started-at[/red] {value!r}; "
            "expected ISO-8601, e.g. 2026-07-15T09:00:00+00:00."
        )
        raise typer.Exit(1) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


@app.command()
def detect(
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30d.")] = "30d",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout.")] = False,
    show_healthy: Annotated[
        bool, typer.Option("--show-healthy", help="Include tests with no findings.")
    ] = False,
) -> None:
    """Run the deterministic detector. Never calls a model."""
    detections = _run_detection(since)
    if json_output:
        stdout.print_json(render_json(detections, llm_enabled=False))
    else:
        render_terminal(detections, stdout, show_healthy=show_healthy)


@app.command()
def triage(
    sha: Annotated[str | None, typer.Option("--sha", help="Commit SHA to triage.")] = None,
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Deterministic output only; zero API calls.")
    ] = False,
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30d.")] = "30d",
    budget_usd: Annotated[
        float | None, typer.Option("--budget-usd", help="Per-invocation cost ceiling.")
    ] = None,
    max_tests: Annotated[
        int | None, typer.Option("--max-tests", help="Cap on tests classified.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", help="terminal, json, or markdown.")
    ] = "terminal",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write to this file as UTF-8 instead of stdout."),
    ] = None,
) -> None:
    """Detect flakes and, unless --no-llm is given, classify their likely cause."""
    del budget_usd, max_tests  # consumed by the classifier in phase P6
    detections = _run_detection(since, sha=sha)

    if not no_llm:
        # The classifier is not built yet. Saying so is better than silently
        # producing deterministic-only output that looks like a full triage.
        stderr.print(
            "[yellow]note[/yellow] the cause classifier arrives in phase P4; "
            "reporting deterministic results only. Pass --no-llm to silence this."
        )

    _emit(detections, output_format, llm_enabled=False, out=out)


@app.command()
def report(
    output_format: Annotated[
        str, typer.Option("--format", help="terminal, json, or markdown.")
    ] = "terminal",
    since: Annotated[str, typer.Option("--since", help="Lookback window, e.g. 30d.")] = "30d",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write to this file as UTF-8 instead of stdout."),
    ] = None,
) -> None:
    """Render the current detection state in the requested format."""
    _emit(_run_detection(since), output_format, llm_enabled=False, out=out)


def _run_detection(since: str, *, sha: str | None = None) -> list[Detection]:
    """Shared detection path for detect, triage and report.

    One code path means the three commands cannot disagree about a verdict, which
    they would eventually do if each assembled its own pipeline.
    """
    try:
        cutoff = cutoff_iso(since)
    except InvalidWindowError as exc:
        stderr.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    store_path = state.config.store_path()
    if not store_path.exists():
        stderr.print(f"[red]No run store at[/red] {store_path}. Run `flaketriage ingest` first.")
        raise typer.Exit(1)

    with RunStore.open(store_path) as store:
        identity_ids = None
        if sha is not None:
            identity_ids = store.failing_identities_at_sha(sha)
            if not identity_ids:
                stderr.print(f"[dim]No failures recorded at {sha}.[/dim]")
        return detect_all(
            store, config=state.config.detect, identity_ids=identity_ids, since=cutoff
        )


def _emit(
    detections: list[Detection],
    output_format: str,
    *,
    llm_enabled: bool,
    out: Path | None = None,
) -> None:
    """Write the report in the requested format, to ``out`` or to stdout."""
    fmt = output_format.strip().lower()

    if fmt == "terminal":
        if out is not None:
            stderr.print("[red]--out is only meaningful with --format json or markdown.[/red]")
            raise typer.Exit(1)
        render_terminal(detections, stdout)
        return

    if fmt == "json":
        text = render_json(detections, llm_enabled=llm_enabled) + "\n"
    elif fmt == "markdown":
        text = render_markdown(
            detections,
            max_rows=state.config.report.pr_comment_max_rows,
            llm_enabled=llm_enabled,
        )
    else:
        stderr.print(
            f"[red]Unknown format[/red] {output_format!r}; expected terminal, json or markdown."
        )
        raise typer.Exit(1)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
        stderr.print(f"[dim]wrote {len(text)} bytes to {out}[/dim]")
        return

    # Written as UTF-8 bytes rather than through the Console: machine-readable
    # output must not be word-wrapped, style-escaped, or re-encoded into whatever
    # code page the terminal happens to be using.
    _write_raw(text)


def _write_raw(text: str) -> None:
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:  # pragma: no cover - only under a captured text stream
        sys.stdout.write(text)
        return
    buffer.write(text.encode("utf-8"))
    buffer.flush()


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
