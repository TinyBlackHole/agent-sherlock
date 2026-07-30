from __future__ import annotations

import argparse

from agent_sherlock.commands import (
    connections_discord,
    connections_gmail,
)
from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections_shared import (
    is_interactive_terminal,
    read_menu_choice,
)
from agent_sherlock.commands.watch import run as run_all_inputs


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(connections_parser=parser)
    providers = parser.add_subparsers(dest="provider", metavar="<provider>")
    connections_discord.configure(providers)
    connections_gmail.configure(providers)


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "connection_handler", None)
    if handler is not None:
        return handler(args)

    if is_interactive_terminal():
        provider_menu = getattr(args, "provider_menu", None)
        if provider_menu is not None:
            return provider_menu()
        return run_interactive_menu()

    parser = getattr(args, "provider_parser", None) or getattr(
        args,
        "connections_parser",
        None,
    )
    if parser is not None:
        parser.print_help()
    return 0


def run_interactive_menu() -> int:
    print("Agent Sherlock connections")
    print("  1. Gmail")
    print("  2. Discord")
    print("  3. Watch all active inputs")
    print("  q. Quit")

    handlers = {
        "1": connections_gmail.run_menu,
        "2": connections_discord.run_menu,
        "3": _run_all_inputs,
    }
    while True:
        choice = read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, or q.")


def _run_all_inputs() -> int:
    return run_all_inputs(
        argparse.Namespace(
            interval=connections_gmail.DEFAULT_POLL_INTERVAL_SECONDS,
        )
    )


COMMAND = Command(
    name="connections",
    help="Connect and watch message inputs.",
    handler=run,
    configure=configure,
)
