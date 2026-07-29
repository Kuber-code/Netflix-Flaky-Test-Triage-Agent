"""Command-line interface.

Command surface is fixed by the specification (§6.7) and declared in full here
from phase P0 onward, with unimplemented commands failing explicitly. A stub
that prints nothing and exits 0 is worse than no command at all: it makes a
missing feature look like an empty result.

stdout carries report data; stderr carries logs and diagnostics.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console

from flaketriage import __version__
from flaketriage.config import Config, load_config
from flaketriage.obs import configure_logging

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
        list[Path] | None,
        typer.Option("--results", help="JUnit XML files or globs to ingest."),
    ] = None,
    sha: Annotated[str | None, typer.Option("--sha", help="Commit SHA under test.")] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="CI run identifier.")] = None,
    attempt: Annotated[int, typer.Option("--attempt", help="Retry attempt number.")] = 1,
) -> None:
    """Parse test results and persist them to the run store."""
    del results, sha, run_id, attempt
    _not_implemented("ingest", "P1")


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
