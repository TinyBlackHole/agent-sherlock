from __future__ import annotations

import argparse
import json
import math
import sys
import threading
import time
from pathlib import Path

from agent_sherlock.application import (
    MessagePipeline,
    PendingDeliveryError,
    PipelineError,
    SyncResult,
)
from agent_sherlock.commands.connections_shared import (
    automatic_watch_status,
    disable_automatic_watch,
    enable_automatic_watch,
    is_interactive_terminal,
    print_error,
    read_menu_choice,
    terminal_safe,
    write_terminal,
)
from agent_sherlock.connectors.gmail import GmailConnector
from agent_sherlock.destinations.active import (
    DESTINATION_API_ERRORS,
    DESTINATION_ERRORS,
    ActiveDestination,
    active_destination_name,
)
from agent_sherlock.input_settings import (
    InputSettingsError,
    input_is_enabled,
    set_input_enabled,
)
from agent_sherlock.integrations.gmail import (
    GmailAPIError,
    GmailConfigurationError,
    GmailError,
    GmailStatus,
    connect_gmail,
    gmail_status,
    open_gmail_mailbox,
)
from agent_sherlock.persistence import MessageRepository, PersistenceError

DEFAULT_POLL_INTERVAL_SECONDS = 30.0
MAX_RETRY_DELAY_SECONDS = 300.0


def configure(providers: argparse._SubParsersAction) -> None:
    gmail = providers.add_parser(
        "gmail",
        help="Connect Gmail and fetch new inbox messages.",
        description="Connect Gmail and fetch new inbox messages.",
    )
    gmail.set_defaults(provider_parser=gmail, provider_menu=run_menu)
    actions = gmail.add_subparsers(dest="action", metavar="<action>")

    connect = actions.add_parser(
        "connect",
        help="Authorize read-only access to incoming Gmail messages.",
        description=(
            "Authorize read-only access to incoming Gmail messages. "
            "Sherlock cannot modify or send email."
        ),
    )
    connect.add_argument(
        "--credentials",
        help="Google OAuth Desktop client credentials JSON file.",
    )
    connect.set_defaults(connection_handler=run_connect)

    fetch = actions.add_parser(
        "fetch",
        help="Fetch new Gmail messages and deliver them to the output.",
        description="Fetch new Gmail messages and deliver them to the output.",
    )
    fetch.add_argument(
        "--json",
        action="store_true",
        help="Print a machine-readable operational result.",
    )
    fetch.set_defaults(connection_handler=run_fetch)

    watch = actions.add_parser(
        "watch",
        help="Continuously forward new Gmail messages to the output.",
        description="Continuously forward new Gmail messages to the output.",
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
        help="Print each batch as a machine-readable operational result.",
    )
    watch.set_defaults(connection_handler=run_watch)

    status = actions.add_parser(
        "status",
        help="Show the local Gmail connection status.",
        description="Show the local Gmail connection status.",
    )
    status.set_defaults(connection_handler=run_status)

    enable = actions.add_parser(
        "enable",
        help="Include Gmail in the automatic watcher.",
        description="Include Gmail when `sherlock watch` starts connected inputs.",
    )
    enable.set_defaults(connection_handler=run_enable)

    disable = actions.add_parser(
        "disable",
        help="Pause automatic Gmail watching without disconnecting it.",
        description="Pause automatic Gmail watching without removing credentials.",
    )
    disable.set_defaults(connection_handler=run_disable)


def run_menu() -> int:
    print("Agent Sherlock Gmail")
    print("  1. Connect Gmail")
    print("  2. Fetch new Gmail messages")
    print("  3. Watch for new Gmail messages")
    print("  4. Show Gmail status")
    print("  5. Enable automatic watching")
    print("  6. Pause automatic watching")
    print("  q. Quit")

    handlers = {
        "1": lambda: run_connect(argparse.Namespace(credentials=None)),
        "2": lambda: run_fetch(argparse.Namespace(json=False)),
        "3": lambda: run_watch(
            argparse.Namespace(
                interval=DEFAULT_POLL_INTERVAL_SECONDS,
                json=False,
            )
        ),
        "4": lambda: run_status(argparse.Namespace()),
        "5": lambda: run_enable(argparse.Namespace()),
        "6": lambda: run_disable(argparse.Namespace()),
    }
    while True:
        choice = read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, 4, 5, 6, or q.")


def _credentials_path_from_args(args: argparse.Namespace) -> Path | None:
    configured_path = getattr(args, "credentials", None)
    if configured_path:
        return Path(configured_path).expanduser()
    if not is_interactive_terminal():
        return None
    try:
        entered_path = input("Path to Google OAuth Desktop credentials JSON: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    return Path(entered_path).expanduser() if entered_path else None


def run_connect(args: argparse.Namespace) -> int:
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
        print_error(exc)
        return 2
    except GmailError as exc:
        print_error(exc)
        return 1

    print(f"Gmail connected: {terminal_safe(profile.email_address)}")
    print("Incoming email can be read; Sherlock cannot modify or send email.")
    try:
        set_input_enabled("gmail", True)
    except InputSettingsError as exc:
        print(
            "Warning: Gmail is connected, but automatic watching could not be "
            f"enabled: {exc}",
            file=sys.stderr,
        )
        return 1

    return 0


def _print_sync_result(
    result: SyncResult,
    *,
    json_output: bool,
    quiet_when_empty: bool = False,
) -> None:
    if json_output:
        should_print = not quiet_when_empty or any(
            (
                result.discovered,
                result.stored,
                result.delivered,
                result.dead_lettered,
                result.initialized,
                result.history_reset,
            )
        )
        if should_print:
            write_terminal(
                json.dumps(
                    {
                        "delivered": result.delivered,
                        "dead_lettered": result.dead_lettered,
                        "discovered": result.discovered,
                        "history_reset": result.history_reset,
                        "initialized": result.initialized,
                        "stored": result.stored,
                        "truncated": result.truncated,
                    },
                    sort_keys=True,
                ),
            )
        return

    if result.initialized:
        write_terminal(
            "Gmail baseline saved. Future messages will be sent to the "
            "configured output."
        )
        return
    if result.history_reset:
        write_terminal(
            "Gmail history was no longer available. A new baseline was saved; "
            "messages from the history gap cannot be identified safely.",
            file=sys.stderr,
        )
        return
    if result.delivered:
        noun = "message" if result.delivered == 1 else "messages"
        write_terminal(f"Sent {result.delivered} {noun} to the configured output.")
    if result.truncated:
        noun = "message was" if result.truncated == 1 else "messages were"
        write_terminal(
            f"Warning: {result.truncated} delivered {noun} truncated; the complete "
            "stored body stays in the local inbox.",
            file=sys.stderr,
        )
    if result.dead_lettered:
        noun = "message" if result.dead_lettered == 1 else "messages"
        write_terminal(
            f"Warning: moved {result.dead_lettered} {noun} to the dead-letter "
            "queue after repeated delivery failures.",
            file=sys.stderr,
        )
    if result.delivered or result.dead_lettered:
        return
    if not result.discovered:
        if not quiet_when_empty:
            write_terminal("No new Gmail messages.")
        return
    if not quiet_when_empty:
        write_terminal("Gmail messages were already queued or delivered.")


def _open_pipeline() -> tuple[GmailConnector, MessagePipeline]:
    connector = GmailConnector(open_gmail_mailbox())
    pipeline = MessagePipeline(
        MessageRepository(),
        ActiveDestination.open(),
    )
    return connector, pipeline


def run_fetch(args: argparse.Namespace) -> int:
    try:
        connector, pipeline = _open_pipeline()
        result = pipeline.sync(connector)
    except (GmailError, *DESTINATION_ERRORS, PersistenceError, PipelineError) as exc:
        print_error(exc)
        return 1

    _print_sync_result(result, json_output=getattr(args, "json", False))
    return 0


def validate_interval(interval: float) -> bool:
    if not math.isfinite(interval) or interval <= 0:
        print(
            "Error: --interval must be a finite number greater than 0.",
            file=sys.stderr,
        )
        return False
    return True


def _retry_delay(
    exception: Exception,
    *,
    interval: float,
    consecutive_errors: int,
) -> float:
    retry_after = getattr(exception, "retry_after", None)
    if retry_after is not None:
        try:
            provider_delay = float(retry_after)
        except (TypeError, ValueError):
            provider_delay = math.nan
        if math.isfinite(provider_delay):
            return max(0.0, min(provider_delay, MAX_RETRY_DELAY_SECONDS))
    return min(
        interval * (2 ** (consecutive_errors - 1)),
        MAX_RETRY_DELAY_SECONDS,
    )


def run_watch(args: argparse.Namespace) -> int:
    if not validate_interval(args.interval):
        return 2

    try:
        connector, pipeline = _open_pipeline()
        destination_name = active_destination_name()
    except (GmailError, *DESTINATION_ERRORS, PersistenceError) as exc:
        print_error(exc)
        return 1

    json_output = getattr(args, "json", False)
    if not json_output:
        print(
            f"Forwarding Gmail to {destination_name} every "
            f"{args.interval:g} seconds. Press Ctrl+C to stop."
        )

    return watch_connected_gmail(
        connector,
        pipeline,
        interval=args.interval,
        json_output=json_output,
    )


def watch_connected_gmail(
    connector: GmailConnector,
    pipeline: MessagePipeline,
    *,
    interval: float,
    json_output: bool = False,
    stop_event: threading.Event | None = None,
) -> int:
    """Watch an already opened Gmail input until interrupted or stopped."""
    consecutive_errors = 0
    try:
        while stop_event is None or not stop_event.is_set():
            try:
                result = pipeline.sync(connector)
                _print_sync_result(
                    result,
                    json_output=json_output,
                    quiet_when_empty=True,
                )
                consecutive_errors = 0
                delay = interval
            except PendingDeliveryError as exc:
                consecutive_errors += 1
                delay = _retry_delay(
                    exc,
                    interval=interval,
                    consecutive_errors=consecutive_errors,
                )
                write_terminal(
                    f"Error: {exc} Delivery remains queued; retrying in "
                    f"{delay:g} seconds.",
                    file=sys.stderr,
                )
            except (GmailAPIError, *DESTINATION_API_ERRORS) as exc:
                if not exc.retryable:
                    print_error(exc)
                    return 1
                consecutive_errors += 1
                delay = _retry_delay(
                    exc,
                    interval=interval,
                    consecutive_errors=consecutive_errors,
                )
                write_terminal(
                    f"Error: {exc} Retrying in {delay:g} seconds.",
                    file=sys.stderr,
                )
            except (
                GmailError,
                *DESTINATION_ERRORS,
                PersistenceError,
                PipelineError,
            ) as exc:
                print_error(exc)
                return 1
            if stop_event is not None:
                if stop_event.wait(delay):
                    break
            else:
                time.sleep(delay)
    except KeyboardInterrupt:
        if not json_output:
            write_terminal("Stopped Gmail watch.")
        return 0
    return 0


def _status_message(status: GmailStatus) -> str:
    if not status.connected:
        return "Gmail is not connected."
    if status.email_address:
        return f"Gmail is connected: {terminal_safe(status.email_address)}"
    return "Gmail has a local authorization token. Run fetch to verify it."


def run_status(_args: argparse.Namespace) -> int:
    try:
        status = gmail_status()
        dead_letters = MessageRepository().dead_letter_count()
        enabled = input_is_enabled("gmail")
    except (GmailError, InputSettingsError, PersistenceError) as exc:
        print_error(exc)
        return 1
    print(_status_message(status))
    print(automatic_watch_status(connected=status.connected, enabled=enabled))
    noun = "message" if dead_letters == 1 else "messages"
    print(f"Dead-letter queue: {dead_letters} {noun}.")
    return 0


def run_enable(_args: argparse.Namespace) -> int:
    return enable_automatic_watch(
        "gmail",
        "Gmail",
        is_connected=lambda: gmail_status().connected,
        provider_errors=(GmailError,),
    )


def run_disable(_args: argparse.Namespace) -> int:
    return disable_automatic_watch("gmail", "Gmail")
