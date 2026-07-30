"""Command-line interface.

Command surface is fixed by the specification (§6.7) and declared in full here
from phase P0 onward, with unimplemented commands failing explicitly. A stub
that prints nothing and exits 0 is worse than no command at all: it makes a
missing feature look like an empty result.

stdout carries report data; stderr carries logs and diagnostics.
"""

from __future__ import annotations

import json
import subprocess  # fixed argv, shell=False
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.table import Table

from flaketriage import __version__
from flaketriage.classify import build_classifier
from flaketriage.config import Config, api_key_from_env, load_config
from flaketriage.detect import Detection, detect_all
from flaketriage.ingest import expand_result_paths, parse_diff_file
from flaketriage.ingest import ingest as run_ingest
from flaketriage.models import Classification, RunMetadata
from flaketriage.obs import as_dict as metrics_as_dict
from flaketriage.obs import configure_logging, render_prometheus
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
    detections = _run_detection(since, sha=sha)

    if no_llm:
        _emit(detections, output_format, llm_enabled=False, out=out)
        return

    if api_key_from_env() is None:
        # A missing key is a mode, not an error -- but it must be visible, or the
        # report looks like a full triage that simply found no causes.
        stderr.print(
            "[yellow]note[/yellow] no ANTHROPIC_API_KEY in the environment; "
            "reporting deterministic results only."
        )
        _emit(detections, output_format, llm_enabled=False, out=out)
        return

    classifier = build_classifier(state.config, budget_usd=budget_usd)
    candidates = [detection for detection in detections if detection.needs_classification]
    classifications = classifier.classify_many(candidates, max_tests=max_tests)

    stats = classifier.stats
    stderr.print(
        f"[dim]classified {len(candidates)} test(s): {stats.api_calls} API call(s), "
        f"{stats.cache_hits} cache hit(s), {stats.prefiltered} prefiltered, "
        f"${stats.total_cost_usd:.4f}[/dim]"
    )

    # Persisted so that `stats` can answer "what does this cost us per week", which
    # is the question that decides whether the tool stays switched on.
    with RunStore.open(state.config.store_path()) as store:
        store.record_metrics(
            stats.as_call_metrics(),
            classifications,
            commit_sha=sha or "",
            cache_hits=frozenset(classifier.cache_hit_ids),
        )

    _emit(
        detections,
        output_format,
        llm_enabled=True,
        out=out,
        classifications=classifications,
    )


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
    classifications: dict[int, Classification] | None = None,
) -> None:
    """Write the report in the requested format, to ``out`` or to stdout."""
    fmt = output_format.strip().lower()

    if fmt == "terminal":
        if out is not None:
            stderr.print("[red]--out is only meaningful with --format json or markdown.[/red]")
            raise typer.Exit(1)
        render_terminal(detections, stdout, classifications=classifications)
        return

    if fmt == "json":
        text = (
            render_json(detections, llm_enabled=llm_enabled, classifications=classifications) + "\n"
        )
    elif fmt == "markdown":
        text = render_markdown(
            detections,
            max_rows=state.config.report.pr_comment_max_rows,
            llm_enabled=llm_enabled,
            classifications=classifications,
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
    no_llm: Annotated[
        bool, typer.Option("--no-llm", help="Baseline only; makes zero API calls.")
    ] = False,
) -> None:
    """Run the evaluation harness against the labeled corpus.

    Delegates to ``eval/run_eval.py`` rather than reimplementing it, so `make eval`
    and this command cannot produce different numbers.
    """
    script = Path(__file__).resolve().parents[2] / "eval" / "run_eval.py"
    if not script.is_file():
        stderr.print(
            f"[red]Evaluation harness not found at[/red] {script}. "
            "It ships with the repository, not with the installed package."
        )
        raise typer.Exit(1)

    argv = [sys.executable, str(script)]
    if subset:
        argv += ["--subset", subset]
    if no_llm:
        argv.append("--no-llm")

    completed = subprocess.run(argv, cwd=script.parent, check=False)  # noqa: S603
    raise typer.Exit(completed.returncode)


@app.command()
def stats(
    since: Annotated[
        str, typer.Option("--since", help="Window to aggregate over, e.g. 30d.")
    ] = "30d",
    json_output: Annotated[bool, typer.Option("--json", help="Emit JSON to stdout.")] = False,
    metrics_out: Annotated[
        Path | None,
        typer.Option(
            "--metrics-out",
            help="Write Prometheus text-format metrics to this file so the tool could "
            "be scraped in a real deployment.",
        ),
    ] = None,
) -> None:
    """Show run metrics: causes, abstention rate, cost, cache hit rate, latency."""
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
        summary = store.metrics_summary(since=cutoff)

    if metrics_out is not None:
        metrics_out.parent.mkdir(parents=True, exist_ok=True)
        metrics_out.write_text(render_prometheus(summary), encoding="utf-8", newline=chr(10))
        stderr.print(f"[dim]wrote Prometheus metrics to {metrics_out}[/dim]")

    if json_output:
        _write_raw(json.dumps(metrics_as_dict(summary), indent=2) + chr(10))
        return

    table = Table(title=f"flaketriage metrics, last {since}", box=None)
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("runs ingested", str(summary.runs))
    table.add_row("classifications", str(summary.classifications))
    table.add_row("abstentions", f"{summary.abstentions} ({summary.abstention_rate:.1%})")
    table.add_row("cache hit rate", f"{summary.cache_hit_rate:.1%}")
    table.add_row("API calls", str(summary.api_calls))
    table.add_row("failed API calls", str(summary.errors))
    table.add_row("total cost", f"${summary.total_cost_usd:.4f}")
    table.add_row("cost per classification", f"${summary.cost_per_classification_usd:.4f}")
    table.add_row(
        "tokens in / out", f"{summary.total_input_tokens} / {summary.total_output_tokens}"
    )
    table.add_row("classify latency P50", f"{summary.latency_p50_ms:.0f} ms")
    table.add_row("classify latency P95", f"{summary.latency_p95_ms:.0f} ms")
    stdout.print(table)

    if summary.by_cause:
        causes = Table(title="classifications by cause", box=None)
        causes.add_column("cause")
        causes.add_column("n", justify="right")
        for cause, count in sorted(summary.by_cause.items(), key=lambda pair: -pair[1]):
            causes.add_row(cause, str(count))
        stdout.print()
        stdout.print(causes)

    # Abstentions are broken down by reason, so a high rate can be diagnosed
    # rather than merely observed.
    if summary.by_downgrade_reason:
        reasons = Table(title="abstentions by reason", box=None)
        reasons.add_column("reason")
        reasons.add_column("n", justify="right")
        for reason, count in sorted(summary.by_downgrade_reason.items(), key=lambda p: -p[1]):
            reasons.add_row(reason, str(count))
        stdout.print()
        stdout.print(reasons)

    if not summary.classifications and not summary.api_calls:
        stderr.print(
            "[dim]No model activity recorded in this window. "
            "Run `flaketriage triage` with an API key to populate it.[/dim]"
        )


if __name__ == "__main__":  # pragma: no cover
    app()
