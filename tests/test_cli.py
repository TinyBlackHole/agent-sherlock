import argparse

import pytest

from agent_sherlock import __version__
from agent_sherlock.cli import build_parser, main
from agent_sherlock.commands.base import Command


def test_short_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["-v"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{__version__}\n"


def test_long_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out == f"{__version__}\n"


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "usage: sherlock" in output
    assert "watch" in output


def _ping_command(exit_code: int = 0) -> tuple[Command, list[argparse.Namespace]]:
    seen: list[argparse.Namespace] = []

    def run(args: argparse.Namespace) -> int:
        seen.append(args)
        return exit_code

    return Command(name="ping", help="Test command.", handler=run), seen


def test_dispatch_invokes_handler_and_returns_its_code():
    command, seen = _ping_command(exit_code=7)
    assert main(["ping"], commands=[command]) == 7
    assert len(seen) == 1


def test_registered_command_appears_in_help(capsys):
    command, _ = _ping_command()
    build_parser([command])  # subparser registration should not raise
    with pytest.raises(SystemExit) as exc:
        main(["--help"], commands=[command])
    assert exc.value.code == 0
    assert "ping" in capsys.readouterr().out
