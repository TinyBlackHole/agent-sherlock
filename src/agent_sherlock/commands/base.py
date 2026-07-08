from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import dataclass

# A handler receives the parsed args and returns a process exit code.
Handler = Callable[[argparse.Namespace], int]
# Optional hook to declare a command's own arguments on its subparser.
Configure = Callable[[argparse.ArgumentParser], None]


@dataclass(frozen=True)
class Command:
    """A single ``sherlock <name>`` subcommand.

    Create one of these per command and add it to ``COMMANDS`` in
    ``agent_sherlock.commands``. See ``_template.py`` for a starting point.
    """

    name: str
    help: str
    handler: Handler
    configure: Configure | None = None

    def register(self, subparsers: argparse._SubParsersAction) -> None:
        """Attach this command's parser and handler to ``subparsers``."""
        parser = subparsers.add_parser(
            self.name,
            help=self.help,
            description=self.help,
        )
        if self.configure is not None:
            self.configure(parser)
        parser.set_defaults(handler=self.handler)
