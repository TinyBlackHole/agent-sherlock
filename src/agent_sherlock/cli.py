from __future__ import annotations

import argparse
from collections.abc import Sequence

from agent_sherlock import __version__
from agent_sherlock.commands import COMMANDS
from agent_sherlock.commands.base import Command


def build_parser(commands: Sequence[Command] = COMMANDS) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sherlock",
        description="Agent Sherlock command-line app.",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )

    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    for command in commands:
        command.register(subparsers)

    return parser


def main(
    argv: Sequence[str] | None = None, commands: Sequence[Command] = COMMANDS
) -> int:
    parser = build_parser(commands)
    args = parser.parse_args(argv)

    # No subcommand given: show help. Every subcommand sets `handler`.
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
