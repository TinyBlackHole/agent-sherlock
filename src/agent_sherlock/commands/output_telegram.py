from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from agent_sherlock.commands.connections_shared import (
    is_interactive_terminal,
    print_error,
    read_menu_choice,
    terminal_safe,
)
from agent_sherlock.destinations.telegram import TelegramDestination
from agent_sherlock.integrations.telegram import (
    TelegramAuthorization,
    TelegramError,
    TelegramStatus,
    connect_telegram,
    telegram_status,
)

MAX_TOKEN_FILE_BYTES = 4_096


def configure(outputs: argparse._SubParsersAction) -> None:
    telegram = outputs.add_parser(
        "telegram",
        help="Manage the private Telegram output.",
        description="Manage the private Telegram output.",
    )
    telegram.set_defaults(output_parser=telegram, output_menu=run_menu)
    actions = telegram.add_subparsers(dest="action", metavar="<action>")

    connect = actions.add_parser(
        "connect",
        help="Connect a Telegram bot and authorize its private output chat.",
        description=(
            "Connect a Telegram bot and authorize one private chat as Sherlock's "
            "only output."
        ),
    )
    connect.add_argument(
        "--token-file",
        help="File containing the Telegram bot token.",
    )
    connect.add_argument(
        "--chat-id",
        type=int,
        help=(
            "Existing private chat ID. If omitted, Sherlock prints a secure "
            "one-time authorization link."
        ),
    )
    connect.set_defaults(output_handler=run_connect)

    test = actions.add_parser(
        "test",
        help="Send a test message to the configured Telegram chat.",
        description="Send a test message to the configured Telegram chat.",
    )
    test.set_defaults(output_handler=run_test)

    status = actions.add_parser(
        "status",
        help="Show the local Telegram connection status.",
        description="Show the local Telegram connection status.",
    )
    status.set_defaults(output_handler=run_status)


def run_menu() -> int:
    print("Agent Sherlock Telegram output")
    print("  1. Connect Telegram")
    print("  2. Send test message")
    print("  3. Show Telegram status")
    print("  q. Quit")

    handlers = {
        "1": lambda: run_connect(argparse.Namespace(token_file=None, chat_id=None)),
        "2": lambda: run_test(argparse.Namespace()),
        "3": lambda: run_status(argparse.Namespace()),
    }
    while True:
        choice = read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, or q.")


def _token_from_args(args: argparse.Namespace) -> str | None:
    token_file = getattr(args, "token_file", None)
    if token_file:
        path = Path(token_file).expanduser()
        try:
            if not path.is_file() or path.stat().st_size > MAX_TOKEN_FILE_BYTES:
                raise OSError
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            print(
                f"Error: cannot read Telegram bot token file: {path}",
                file=sys.stderr,
            )
            return None
        return token
    if not is_interactive_terminal():
        return None
    try:
        return getpass.getpass("Telegram bot token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _print_authorization(authorization: TelegramAuthorization) -> None:
    print("Open this one-time link and press Start to authorize the private chat:")
    print(authorization.url)
    print("Waiting for Telegram authorization…")


def run_connect(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    if not token:
        print(
            "Error: enter a bot token interactively or provide --token-file.",
            file=sys.stderr,
        )
        return 2
    try:
        status = connect_telegram(
            token,
            chat_id=getattr(args, "chat_id", None),
            on_authorization=_print_authorization,
        )
    except TelegramError as exc:
        print_error(exc)
        return 1

    print(f"Telegram connected: @{terminal_safe(status.bot_username)}")
    return 0


def run_test(_args: argparse.Namespace) -> int:
    try:
        TelegramDestination.open().send("Agent Sherlock Telegram test successful.")
    except TelegramError as exc:
        print_error(exc)
        return 1
    print("Test message sent to Telegram.")
    return 0


def _status_message(status: TelegramStatus) -> str:
    if not status.connected:
        return "Telegram is not connected."
    return f"Telegram is connected: @{terminal_safe(status.bot_username)}"


def run_status(_args: argparse.Namespace) -> int:
    try:
        status = telegram_status()
    except TelegramError as exc:
        print_error(exc)
        return 1
    print(_status_message(status))
    return 0
