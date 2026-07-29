from __future__ import annotations

import pytest
from typer.testing import CliRunner

from flaketriage import __version__
from flaketriage.cli import app

runner = CliRunner()

# The command surface fixed by the specification (§6.7). This test exists so
# that renaming or dropping a command is a deliberate, visible change.
EXPECTED_COMMANDS = frozenset({"ingest", "detect", "triage", "report", "policy", "eval", "stats"})

# Commands whose behaviour is not built yet. They must fail explicitly rather
# than exit 0 and look like an empty result. Shrinks as phases land.
UNIMPLEMENTED_COMMANDS = frozenset({"policy", "eval", "stats"})


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_every_specified_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in EXPECTED_COMMANDS:
        assert command in result.stdout


def test_bare_invocation_shows_help_and_does_not_crash() -> None:
    result = runner.invoke(app, [])
    # Click signals "no command given" with exit code 2 after printing help.
    assert result.exit_code in {0, 2}
    assert "Usage" in result.output
    assert not isinstance(result.exception, Exception)


@pytest.mark.parametrize("command", sorted(UNIMPLEMENTED_COMMANDS))
def test_unimplemented_commands_fail_explicitly(command: str) -> None:
    """An unimplemented command must not exit 0 and look like an empty result."""
    result = runner.invoke(app, [command])
    assert result.exit_code == 2, result.output
    assert "not implemented" in result.output


@pytest.mark.parametrize("command", sorted(EXPECTED_COMMANDS))
def test_every_command_has_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0
    assert "Usage" in result.stdout


def test_unknown_command_is_an_error() -> None:
    assert runner.invoke(app, ["nope"]).exit_code != 0
