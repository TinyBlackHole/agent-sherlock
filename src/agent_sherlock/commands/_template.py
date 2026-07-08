"""Template for a new subcommand. Copy this file, then:

1. Rename it (e.g. ``scan.py``) and fill in ``name`` / ``help``.
2. Declare any arguments in ``configure``.
3. Do the work in ``run`` and return an exit code (0 = success).
4. Register it in ``agent_sherlock/commands/__init__.py``:

       from agent_sherlock.commands.scan import COMMAND as scan
       COMMANDS = [scan]

This module is a template only and is intentionally not registered.
"""

from __future__ import annotations

import argparse

from agent_sherlock.commands.base import Command


def configure(parser: argparse.ArgumentParser) -> None:
    """Declare this command's arguments."""
    parser.add_argument("target", help="What to investigate.")


def run(args: argparse.Namespace) -> int:
    """Run the command and return a process exit code."""
    print(f"Investigating {args.target}...")
    return 0


COMMAND = Command(
    name="example",
    help="One-line description shown in `sherlock --help`.",
    handler=run,
    configure=configure,
)
