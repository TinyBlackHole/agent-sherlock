from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from agent_sherlock.commands.base import Command
from agent_sherlock.integrations.gmail import (
    GmailAPIError,
    GmailConfigurationError,
    GmailError,
    GmailFetchResult,
    GmailPaths,
    GmailStatus,
    connect_gmail,
    fetch_new_gmail_messages,
    gmail_status,
    open_gmail_mailbox,
)

DEFAULT_POLL_INTERVAL_SECONDS = 30.0
MAX_RETRY_DELAY_SECONDS = 300.0
MAX_TERMINAL_FIELD_CHARACTERS = 1_000


def gmail_config_dir() -> Path:
    return GmailPaths.default().directory


def gmail_token_path() -> Path:
    return GmailPaths.default().token


def gmail_state_path() -> Path:
    return GmailPaths.default().state


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(connections_parser=parser)
    providers = parser.add_subparsers(dest="provider", metavar="<provider>")

    gmail = providers.add_parser(
        "gmail",
        help="Connect Gmail and fetch new inbox messages.",
        description="Connect Gmail and fetch new inbox messages.",
    )
    gmail.set_defaults(gmail_parser=gmail)
    gmail_actions = gmail.add_subparsers(dest="action", metavar="<action>")

    connect = gmail_actions.add_parser(
        "connect",
        help="Authorize read-only access to Gmail metadata.",
        description=(
            "Authorize read-only access to Gmail headers and labels. "
            "Message bodies are not requested."
        ),
    )
    connect.add_argument(
        "--credentials",
        help="Google OAuth Desktop client credentials JSON file.",
    )
    connect.set_defaults(connection_handler=run_gmail_connect)

    fetch = gmail_actions.add_parser(
        "fetch",
        help="Fetch Gmail messages received since the previous check.",
        description="Fetch Gmail messages received since the previous check.",
    )
    fetch.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable JSON result.",
    )
    fetch.set_defaults(connection_handler=run_gmail_fetch)

    watch = gmail_actions.add_parser(
        "watch",
        help="Continuously fetch new Gmail inbox messages.",
        description="Continuously fetch new Gmail inbox messages.",
    )
    watch.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            f"Seconds between Gmail checks. Default: {DEFAULT_POLL_INTERVAL_SECONDS:g}."
        ),
    )
    watch.add_argument(
        "--json",
        action="store_true",
        help="Print each batch as machine-readable JSON.",
    )
    watch.set_defaults(connection_handler=run_gmail_watch)

    status = gmail_actions.add_parser(
        "status",
        help="Show the local Gmail connection status.",
        description="Show the local Gmail connection status.",
    )
    status.set_defaults(connection_handler=run_gmail_status)


def _is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "connection_handler", None)
    if handler is not None:
        return handler(args)

    gmail_parser = getattr(args, "gmail_parser", None)
    if _is_interactive_terminal():
        if gmail_parser is not None:
            return run_gmail_menu()
        return run_interactive_menu()

    parser = gmail_parser or getattr(args, "connections_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def _read_menu_choice() -> str:
    try:
        return input("Select an option: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def run_interactive_menu() -> int:
    print("Agent Sherlock connections")
    print("  1. Gmail")
    print("  2. Discord")
    print("  q. Quit")

    handlers = {
        "1": run_gmail_menu,
        "2": run_discord_menu,
    }
    while True:
        choice = _read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, or q.")


def run_gmail_menu() -> int:
    print("Agent Sherlock Gmail")
    print("  1. Connect Gmail")
    print("  2. Fetch new Gmail messages")
    print("  3. Watch for new Gmail messages")
    print("  4. Show Gmail status")
    print("  q. Quit")

    handlers = {
        "1": lambda: run_gmail_connect(argparse.Namespace(credentials=None)),
        "2": lambda: run_gmail_fetch(argparse.Namespace(json=False)),
        "3": lambda: run_gmail_watch(
            argparse.Namespace(
                interval=DEFAULT_POLL_INTERVAL_SECONDS,
                json=False,
            )
        ),
        "4": lambda: run_gmail_status(argparse.Namespace()),
    }
    while True:
        choice = _read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, 4, or q.")


def run_discord_menu() -> int:
    print("Agent Sherlock Discord")
    print("  1. Soon")
    print("  q. Quit")

    while True:
        choice = _read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        if choice == "1":
            print("Discord integration coming soon.")
            return 0
        print("Choose 1 or q.")


def _credentials_path_from_args(args: argparse.Namespace) -> Path | None:
    configured_path = getattr(args, "credentials", None)
    if configured_path:
        return Path(configured_path).expanduser()
    if not _is_interactive_terminal():
        return None
    try:
        entered_path = input("Path to Google OAuth Desktop credentials JSON: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return Path(entered_path).expanduser() if entered_path else None


def _print_error(error: GmailError) -> None:
    print(f"Error: {error}", file=sys.stderr)


def run_gmail_connect(args: argparse.Namespace) -> int:
    credentials_path = _credentials_path_from_args(args)
    if credentials_path is None:
        print(
            "Error: provide --credentials with a Google OAuth Desktop JSON file.",
            file=sys.stderr,
        )
        return 2

    try:
        profile = connect_gmail(credentials_path)
    except GmailConfigurationError as exc:
        _print_error(exc)
        return 2
    except GmailError as exc:
        _print_error(exc)
        return 1

    print(f"Gmail connected: {_terminal_safe(profile.email_address)}")
    print("Only message metadata is authorized; email bodies remain inaccessible.")
    return 0


def _terminal_safe(value: str, *, fallback: str = "(unknown)") -> str:
    """Collapse untrusted mail text and strip terminal control characters."""
    cleaned = "".join(
        character if character.isprintable() else " " for character in value
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_TERMINAL_FIELD_CHARACTERS:
        cleaned = f"{cleaned[: MAX_TERMINAL_FIELD_CHARACTERS - 1]}…"
    return cleaned or fallback


def _result_as_json(result: GmailFetchResult) -> dict[str, Any]:
    return {
        "count": len(result.messages),
        "history_reset": result.history_reset,
        "initialized": result.initialized,
        "messages": [message.as_json() for message in result.messages],
    }


def _print_fetch_result(
    result: GmailFetchResult,
    *,
    json_output: bool,
    quiet_when_empty: bool = False,
) -> None:
    if json_output:
        should_print = (
            not quiet_when_empty
            or result.messages
            or result.initialized
            or result.history_reset
        )
        if should_print:
            print(json.dumps(_result_as_json(result), sort_keys=True))
        return

    if result.initialized:
        print("Gmail baseline saved. Future checks will show newly received messages.")
        return
    if result.history_reset:
        print(
            "Gmail history was no longer available. A new baseline was saved; "
            "messages from the history gap cannot be identified safely.",
            file=sys.stderr,
        )
        return
    if not result.messages:
        if not quiet_when_empty:
            print("No new Gmail messages.")
        return

    for index, message in enumerate(result.messages):
        if index:
            print()
        print("New Gmail message")
        print(f"  From: {_terminal_safe(message.sender)}")
        print(f"  Subject: {_terminal_safe(message.subject, fallback='(no subject)')}")
        print(f"  Date: {_terminal_safe(message.date)}")


def run_gmail_fetch(args: argparse.Namespace) -> int:
    try:
        mailbox = open_gmail_mailbox()
        result = fetch_new_gmail_messages(mailbox)
    except GmailError as exc:
        _print_error(exc)
        return 1

    _print_fetch_result(result, json_output=getattr(args, "json", False))
    return 0


def _validate_interval(interval: float) -> bool:
    if interval <= 0:
        print("Error: --interval must be greater than 0.", file=sys.stderr)
        return False
    return True


def run_gmail_watch(args: argparse.Namespace) -> int:
    if not _validate_interval(args.interval):
        return 2

    try:
        mailbox = open_gmail_mailbox()
    except GmailError as exc:
        _print_error(exc)
        return 1

    json_output = getattr(args, "json", False)
    if not json_output:
        print(f"Watching Gmail every {args.interval:g} seconds. Press Ctrl+C to stop.")

    consecutive_api_errors = 0
    try:
        while True:
            try:
                result = fetch_new_gmail_messages(mailbox)
                _print_fetch_result(
                    result,
                    json_output=json_output,
                    quiet_when_empty=True,
                )
                consecutive_api_errors = 0
                delay = args.interval
            except GmailAPIError as exc:
                if not exc.retryable:
                    _print_error(exc)
                    return 1
                consecutive_api_errors += 1
                delay = min(
                    args.interval * (2 ** (consecutive_api_errors - 1)),
                    MAX_RETRY_DELAY_SECONDS,
                )
                print(
                    f"Error: {exc} Retrying in {delay:g} seconds.",
                    file=sys.stderr,
                )
            except GmailError as exc:
                _print_error(exc)
                return 1
            time.sleep(delay)
    except KeyboardInterrupt:
        if not json_output:
            print("Stopped Gmail watch.")
        return 0


def _status_message(status: GmailStatus) -> str:
    if not status.connected:
        return "Gmail is not connected."
    if status.email_address:
        return f"Gmail is connected: {_terminal_safe(status.email_address)}"
    return "Gmail has a local authorization token. Run fetch to verify it."


def run_gmail_status(_args: argparse.Namespace) -> int:
    try:
        status = gmail_status()
    except GmailError as exc:
        _print_error(exc)
        return 1
    print(_status_message(status))
    return 0


COMMAND = Command(
    name="connections",
    help="Connect external services and fetch their events.",
    handler=run,
    configure=configure,
)
