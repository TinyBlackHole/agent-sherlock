from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from agent_sherlock.application import (
    MessagePipeline,
    PendingDeliveryError,
    PipelineError,
)
from agent_sherlock.commands.connections_shared import (
    is_interactive_terminal,
    print_error,
    read_menu_choice,
    terminal_safe,
)
from agent_sherlock.connectors.discord import DiscordConnector
from agent_sherlock.destinations.telegram import TelegramDestination
from agent_sherlock.integrations.discord import (
    DiscordError,
    DiscordStatus,
    connect_discord,
    discord_status,
    load_discord_credentials,
    watch_discord,
)
from agent_sherlock.integrations.telegram import TelegramError
from agent_sherlock.persistence import MessageRepository, PersistenceError

MAX_TOKEN_FILE_BYTES = 4_096


def configure(providers: argparse._SubParsersAction) -> None:
    discord = providers.add_parser(
        "discord",
        help="Connect a Discord channel as an input.",
        description="Connect a Discord server channel and forward new messages.",
    )
    discord.set_defaults(provider_parser=discord, provider_menu=run_menu)
    actions = discord.add_subparsers(dest="action", metavar="<action>")

    connect = actions.add_parser(
        "connect",
        help="Connect a bot and select one Discord input channel.",
        description="Connect a bot and select one Discord server input channel.",
    )
    connect.add_argument(
        "--token-file",
        help="File containing the Discord bot token.",
    )
    connect.add_argument(
        "--channel-id",
        type=int,
        help="Discord server channel ID to monitor.",
    )
    connect.set_defaults(connection_handler=run_connect)

    watch = actions.add_parser(
        "watch",
        help="Continuously forward new Discord messages to Telegram.",
        description="Continuously forward new Discord messages to Telegram.",
    )
    watch.set_defaults(connection_handler=run_watch)

    status = actions.add_parser(
        "status",
        help="Show the local Discord connection status.",
        description="Show the local Discord connection status.",
    )
    status.set_defaults(connection_handler=run_status)


def run_menu() -> int:
    print("Agent Sherlock Discord")
    print("  1. Connect Discord")
    print("  2. Watch for new Discord messages")
    print("  3. Show Discord status")
    print("  q. Quit")

    handlers = {
        "1": lambda: run_connect(argparse.Namespace(token_file=None, channel_id=None)),
        "2": lambda: run_watch(argparse.Namespace()),
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
                f"Error: cannot read Discord bot token file: {path}",
                file=sys.stderr,
            )
            return None
        return token
    if not is_interactive_terminal():
        return None
    try:
        return getpass.getpass("Discord bot token: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def _channel_id_from_args(args: argparse.Namespace) -> int | None:
    channel_id = getattr(args, "channel_id", None)
    if channel_id is not None:
        return channel_id if channel_id > 0 else None
    if not is_interactive_terminal():
        return None
    try:
        raw_channel_id = input("Discord channel ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    try:
        parsed = int(raw_channel_id)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def run_connect(args: argparse.Namespace) -> int:
    token = _token_from_args(args)
    if not token:
        print(
            "Error: enter a bot token interactively or provide --token-file.",
            file=sys.stderr,
        )
        return 2
    channel_id = _channel_id_from_args(args)
    if channel_id is None:
        print(
            "Error: enter a positive Discord channel ID or provide --channel-id.",
            file=sys.stderr,
        )
        return 2

    try:
        status = connect_discord(token, channel_id=channel_id)
    except DiscordError as exc:
        print_error(exc)
        return 1

    print(
        f"Discord connected: @{terminal_safe(status.bot_username)} watching "
        f"#{terminal_safe(status.channel_name)} ({status.channel_id})."
    )
    return 0


def _open_pipeline() -> tuple[DiscordConnector, MessagePipeline]:
    credentials = load_discord_credentials()
    connector = DiscordConnector(credentials)
    pipeline = MessagePipeline(
        MessageRepository(),
        TelegramDestination.open(),
    )
    return connector, pipeline


def run_watch(_args: argparse.Namespace) -> int:
    try:
        connector, pipeline = _open_pipeline()
    except (DiscordError, TelegramError, PersistenceError) as exc:
        print_error(exc)
        return 1

    credentials = connector.credentials

    def on_ready() -> None:
        print(
            f"Forwarding Discord #{terminal_safe(credentials.channel_name)} "
            "to Telegram. Press Ctrl+C to stop."
        )

    def on_message(message: object) -> None:
        normalized = connector.normalize(message)
        if normalized is None:
            return
        try:
            result = pipeline.ingest((normalized,))
        except PendingDeliveryError as exc:
            print(
                f"Error: {exc} The Discord message remains queued for delivery.",
                file=sys.stderr,
            )
            return
        except (PersistenceError, PipelineError) as exc:
            print(
                f"Error: {exc} Discord watch remains connected; queued work "
                "will be retried.",
                file=sys.stderr,
            )
            return
        _print_delivery_result(
            delivered=result.delivered,
            dead_lettered=result.dead_lettered,
        )

    def on_maintenance() -> None:
        try:
            result = pipeline.deliver_pending()
        except PendingDeliveryError as exc:
            print(
                f"Error: {exc} Queued delivery will be retried.",
                file=sys.stderr,
            )
            return
        except (PersistenceError, PipelineError) as exc:
            print(
                f"Error: {exc} Discord watch remains connected; queued delivery "
                "will be retried.",
                file=sys.stderr,
            )
            return
        _print_delivery_result(
            delivered=result.delivered,
            dead_lettered=result.dead_lettered,
        )

    try:
        watch_discord(
            credentials,
            on_message,
            on_ready_callback=on_ready,
            on_maintenance_callback=on_maintenance,
        )
    except (DiscordError, TelegramError, PersistenceError, PipelineError) as exc:
        print_error(exc)
        return 1
    print("Stopped Discord watch.")
    return 0


def _print_delivery_result(*, delivered: int, dead_lettered: int) -> None:
    if delivered:
        noun = "message" if delivered == 1 else "messages"
        print(f"Sent {delivered} queued {noun} to Telegram.")
    if dead_lettered:
        noun = "message" if dead_lettered == 1 else "messages"
        print(
            f"Warning: moved {dead_lettered} {noun} to the dead-letter queue.",
            file=sys.stderr,
        )


def _status_message(status: DiscordStatus) -> str:
    if not status.connected:
        return "Discord is not connected."
    return (
        f"Discord is connected: @{terminal_safe(status.bot_username)} watching "
        f"#{terminal_safe(status.channel_name)} ({status.channel_id})."
    )


def run_status(_args: argparse.Namespace) -> int:
    try:
        status = discord_status()
        dead_letters = MessageRepository().dead_letter_count()
    except (DiscordError, PersistenceError) as exc:
        print_error(exc)
        return 1
    print(_status_message(status))
    noun = "message" if dead_letters == 1 else "messages"
    print(f"Dead-letter queue: {dead_letters} {noun}.")
    return 0
