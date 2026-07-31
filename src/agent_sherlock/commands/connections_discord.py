from __future__ import annotations

import argparse
import getpass
import sys
import threading
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path

from agent_sherlock.ai import AIConfigurationError, open_message_processor
from agent_sherlock.application import (
    MessageIngestError,
    MessagePipeline,
    PendingDeliveryError,
    PipelineError,
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
from agent_sherlock.connectors.discord import DiscordConnector
from agent_sherlock.destinations.active import (
    DESTINATION_ERRORS,
    ActiveDestination,
    active_destination_name,
)
from agent_sherlock.destinations.discord import DiscordDestination
from agent_sherlock.input_settings import (
    InputSettingsError,
    input_is_enabled,
    set_input_enabled,
)
from agent_sherlock.integrations.discord import (
    DiscordConfigurationError,
    DiscordError,
    DiscordStatus,
    connect_discord,
    discord_status,
    load_discord_credentials,
    watch_discord,
)
from agent_sherlock.persistence import MessageRepository, PersistenceError

MAX_TOKEN_FILE_BYTES = 4_096
DELIVERY_RETRY_INITIAL_SECONDS = 5.0
DELIVERY_RETRY_MAX_SECONDS = 300.0


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
        help="Continuously forward new Discord messages to the output.",
        description="Continuously forward new Discord messages to the output.",
    )
    watch.set_defaults(connection_handler=run_watch)

    status = actions.add_parser(
        "status",
        help="Show the local Discord connection status.",
        description="Show the local Discord connection status.",
    )
    status.set_defaults(connection_handler=run_status)

    enable = actions.add_parser(
        "enable",
        help="Include Discord in the automatic watcher.",
        description="Include Discord when `sherlock watch` starts connected inputs.",
    )
    enable.set_defaults(connection_handler=run_enable)

    disable = actions.add_parser(
        "disable",
        help="Pause automatic Discord watching without disconnecting it.",
        description="Pause automatic Discord watching without removing credentials.",
    )
    disable.set_defaults(connection_handler=run_disable)


def run_menu() -> int:
    print("Agent Sherlock Discord")
    print("  1. Connect Discord")
    print("  2. Watch for new Discord messages")
    print("  3. Show Discord status")
    print("  4. Enable automatic watching")
    print("  5. Pause automatic watching")
    print("  q. Quit")

    handlers = {
        "1": lambda: run_connect(argparse.Namespace(token_file=None, channel_id=None)),
        "2": lambda: run_watch(argparse.Namespace()),
        "3": lambda: run_status(argparse.Namespace()),
        "4": lambda: run_enable(argparse.Namespace()),
        "5": lambda: run_disable(argparse.Namespace()),
    }
    while True:
        choice = read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, 4, 5, or q.")


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
    try:
        set_input_enabled("discord", True)
    except InputSettingsError as exc:
        print(
            "Warning: Discord is connected, but automatic watching could not be "
            f"enabled: {exc}",
            file=sys.stderr,
        )
        return 1

    return 0


def _open_pipeline() -> tuple[DiscordConnector, MessagePipeline]:
    credentials = load_discord_credentials()
    connector = DiscordConnector(credentials)
    destination = ActiveDestination.open(
        validator=partial(reject_delivery_loop, credentials.channel_id),
    )
    pipeline = MessagePipeline(
        MessageRepository(),
        destination,
        processor=open_message_processor(),
    )
    return connector, pipeline


def reject_delivery_loop(watched_channel_id: int, destination: object) -> None:
    """Refuse to watch the same channel the Discord output posts into.

    Delivered messages would be read back as new input and forwarded again,
    so the pair has to be rejected before the Gateway connects.
    """
    if not isinstance(destination, DiscordDestination):
        return
    if destination.credentials.channel_id != watched_channel_id:
        return
    raise DiscordConfigurationError(
        "The watched Discord channel is also the Discord output channel, which "
        "would forward every delivered message back to itself. Watch a "
        "different channel or point the output webhook elsewhere."
    )


def run_watch(_args: argparse.Namespace) -> int:
    try:
        connector, pipeline = _open_pipeline()
        destination_name = active_destination_name()
    except (
        DiscordError,
        *DESTINATION_ERRORS,
        AIConfigurationError,
        PersistenceError,
    ) as exc:
        print_error(exc)
        return 1

    return watch_connected_discord(
        connector,
        pipeline,
        destination_name=destination_name,
    )


def watch_connected_discord(
    connector: DiscordConnector,
    pipeline: MessagePipeline,
    *,
    destination_name: str,
    stop_event: threading.Event | None = None,
    ready_message: str | None = None,
    announce_stop: bool = True,
) -> int:
    """Watch an already opened Discord input until the Gateway stops."""
    credentials = connector.credentials
    message_when_ready = ready_message or (
        f"Forwarding Discord #{terminal_safe(credentials.channel_name)} "
        f"to {destination_name}. Press Ctrl+C to stop."
    )

    schedule = DeliverySchedule()

    def on_ready() -> None:
        write_terminal(message_when_ready)

    def on_message(message: object) -> None:
        normalized = connector.normalize(message)
        if normalized is None:
            return
        try:
            pipeline.store((normalized,))
        except MessageIngestError as exc:
            # The Gateway does not replay events, so continuing here would drop
            # this message for good. Stop loudly instead.
            write_terminal(
                f"Error: {exc} Stopping the Discord watch: the Gateway does not "
                "replay events, so continuing would drop messages silently. "
                "Resolve the storage error, then start the watch again.",
                file=sys.stderr,
            )
            raise
        _deliver_backlog()

    def on_maintenance() -> None:
        _deliver_backlog()

    def _deliver_backlog() -> None:
        """Drain the queue, at most once per backoff window.

        Every Gateway event used to trigger a full backlog delivery, so an output
        that was down got hammered once per incoming message. Failures now push
        the next attempt out exponentially.
        """
        if not schedule.ready():
            return
        try:
            result = pipeline.deliver_pending()
        except PendingDeliveryError as exc:
            delay = schedule.failed(retry_after=_retry_after_seconds(exc))
            _print_pending_delivery_error(exc, retry_in_seconds=delay)
            return
        except (PersistenceError, PipelineError) as exc:
            delay = schedule.failed()
            write_terminal(
                f"Error: {exc} Discord watch remains connected; queued delivery "
                f"will be retried in {delay:.0f}s.",
                file=sys.stderr,
            )
            return
        schedule.succeeded()
        _print_delivery_result(
            delivered=result.delivered,
            dead_lettered=result.dead_lettered,
            truncated=result.truncated,
        )

    try:
        watch_discord(
            credentials,
            on_message,
            on_ready_callback=on_ready,
            on_maintenance_callback=on_maintenance,
            stop_event=stop_event,
        )
    except (DiscordError, *DESTINATION_ERRORS, PersistenceError, PipelineError) as exc:
        print_error(exc)
        return 1
    if announce_stop:
        write_terminal("Stopped Discord watch.")
    return 0


class DeliverySchedule:
    """Exponential backoff for backlog delivery attempts."""

    def __init__(
        self,
        *,
        initial_seconds: float = DELIVERY_RETRY_INITIAL_SECONDS,
        maximum_seconds: float = DELIVERY_RETRY_MAX_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.initial_seconds = initial_seconds
        self.maximum_seconds = maximum_seconds
        self._clock = clock
        self._delay = 0.0
        self._next_attempt_at = 0.0

    def ready(self) -> bool:
        return self._clock() >= self._next_attempt_at

    def succeeded(self) -> None:
        self._delay = 0.0
        self._next_attempt_at = 0.0

    def failed(self, *, retry_after: float | None = None) -> float:
        """Push the next attempt out and return how long that will be."""
        grown = self.initial_seconds if self._delay <= 0 else self._delay * 2
        delay = min(max(grown, retry_after or 0.0), self.maximum_seconds)
        self._delay = delay
        self._next_attempt_at = self._clock() + delay
        return delay


def _retry_after_seconds(exception: PendingDeliveryError) -> float | None:
    retry_after = exception.retry_after
    if isinstance(retry_after, bool) or not isinstance(retry_after, int | float):
        return None
    return max(0.0, float(retry_after))


def _print_pending_delivery_error(
    exception: PendingDeliveryError,
    *,
    retry_in_seconds: float | None = None,
) -> None:
    if isinstance(exception.cause, DiscordConfigurationError):
        guidance = (
            " Change the output configuration to resolve the conflict; the "
            "message remains queued until then."
        )
    elif retry_in_seconds is None:
        guidance = " Queued delivery will be retried."
    else:
        guidance = f" Queued delivery will be retried in {retry_in_seconds:.0f}s."
    write_terminal(f"Error: {exception}{guidance}", file=sys.stderr)


def _print_delivery_result(
    *,
    delivered: int,
    dead_lettered: int,
    truncated: int = 0,
) -> None:
    if delivered:
        noun = "message" if delivered == 1 else "messages"
        write_terminal(f"Sent {delivered} queued {noun} to the configured output.")
    if truncated:
        noun = "message was" if truncated == 1 else "messages were"
        write_terminal(
            f"Warning: {truncated} delivered {noun} truncated; the complete "
            "stored body stays in the local inbox.",
            file=sys.stderr,
        )
    if dead_lettered:
        noun = "message" if dead_lettered == 1 else "messages"
        write_terminal(
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
        enabled = input_is_enabled("discord")
    except (DiscordError, InputSettingsError, PersistenceError) as exc:
        print_error(exc)
        return 1
    print(_status_message(status))
    print(automatic_watch_status(connected=status.connected, enabled=enabled))
    noun = "message" if dead_letters == 1 else "messages"
    print(f"Dead-letter queue: {dead_letters} {noun}.")
    return 0


def run_enable(_args: argparse.Namespace) -> int:
    return enable_automatic_watch(
        "discord",
        "Discord",
        is_connected=lambda: discord_status().connected,
        provider_errors=(DiscordError,),
    )


def run_disable(_args: argparse.Namespace) -> int:
    return disable_automatic_watch("discord", "Discord")
