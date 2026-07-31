from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial

from agent_sherlock.ai import AIConfigurationError, open_message_processor
from agent_sherlock.application import MessagePipeline, PipelineError
from agent_sherlock.commands import connections_discord, connections_gmail
from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections_shared import (
    print_error,
    terminal_safe,
    write_terminal,
)
from agent_sherlock.connectors.discord import DiscordConnector
from agent_sherlock.connectors.gmail import GmailConnector
from agent_sherlock.destinations.active import (
    DESTINATION_ERRORS,
    ActiveDestination,
    active_destination_name,
)
from agent_sherlock.input_settings import (
    InputSettings,
    InputSettingsError,
    load_input_settings,
)
from agent_sherlock.integrations.discord import (
    DiscordCredentials,
    DiscordError,
    discord_status,
    load_discord_credentials,
)
from agent_sherlock.integrations.gmail import (
    GmailError,
    gmail_status,
    open_gmail_mailbox,
)
from agent_sherlock.persistence import MessageRepository, PersistenceError


@dataclass(frozen=True, slots=True)
class _ConnectedInputs:
    gmail: bool = False
    discord: DiscordCredentials | None = None


@dataclass(frozen=True, slots=True)
class _Worker:
    name: str
    target: Callable[[], int]


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--interval",
        type=float,
        default=connections_gmail.DEFAULT_POLL_INTERVAL_SECONDS,
        help=(
            "Seconds between checks for polling inputs such as Gmail. "
            f"Default: {connections_gmail.DEFAULT_POLL_INTERVAL_SECONDS:g}."
        ),
    )


def run(args: argparse.Namespace) -> int:
    interval = getattr(
        args,
        "interval",
        connections_gmail.DEFAULT_POLL_INTERVAL_SECONDS,
    )

    try:
        settings = load_input_settings()
    except InputSettingsError as exc:
        print_error(exc)
        return 1

    connected, discovery_errors = _discover_connected_inputs(settings)
    active_names = [
        name
        for name, is_connected in (
            ("gmail", connected.gmail),
            ("discord", connected.discord is not None),
        )
        if is_connected
    ]
    if not active_names:
        if discovery_errors:
            write_terminal(
                "Error: no input watcher could start because connection "
                "discovery failed. Resolve the errors above and try again.",
                file=sys.stderr,
            )
        else:
            write_terminal(
                "Error: no connected and enabled inputs were found. Connect an "
                "input or enable it under `sherlock connections`.",
                file=sys.stderr,
            )
        return 1
    if connected.gmail and not connections_gmail.validate_interval(interval):
        return 2

    validator = None
    if connected.discord is not None:
        validator = partial(
            connections_discord.reject_delivery_loop,
            connected.discord.channel_id,
        )

    try:
        destination = ActiveDestination.open(validator=validator)
        destination_name = active_destination_name()
        repository = MessageRepository()
        repository.initialize()
        processor = open_message_processor()
    except (
        *DESTINATION_ERRORS,
        AIConfigurationError,
        PersistenceError,
        PipelineError,
    ) as exc:
        print_error(exc)
        return 1

    pipeline = MessagePipeline(repository, destination, processor=processor)
    stop_event = threading.Event()
    workers = _build_workers(
        connected,
        pipeline,
        stop_event=stop_event,
        interval=interval,
        destination_name=destination_name,
    )

    write_terminal(
        f"Forwarding {', '.join(worker.name for worker in workers)} "
        f"to {destination_name}. Press Ctrl+C to stop."
    )
    outcomes: dict[str, int] = {}
    outcome_lock = threading.Lock()

    def supervise(worker: _Worker) -> None:
        try:
            exit_code = worker.target()
        except Exception as exc:
            write_terminal(
                f"Error: {worker.name} watch stopped unexpectedly: {exc}",
                file=sys.stderr,
            )
            exit_code = 1
        with outcome_lock:
            outcomes[worker.name] = exit_code

    threads = [
        threading.Thread(
            target=supervise,
            args=(worker,),
            name=f"sherlock-{worker.name}-watch",
            daemon=True,
        )
        for worker in workers
    ]
    try:
        interrupted, forced = _start_and_wait_for_workers(threads, stop_event)
    finally:
        stop_event.set()

    if forced:
        write_terminal(
            "Second interrupt received; forcing Sherlock watch to exit.",
            file=sys.stderr,
        )
        return 130
    if interrupted:
        write_terminal("Stopped Sherlock watch.")
    elif any(code != 0 for code in outcomes.values()):
        write_terminal(
            "All remaining input watchers have stopped.",
            file=sys.stderr,
        )

    return 1 if discovery_errors or any(outcomes.values()) else 0


def _start_and_wait_for_workers(
    threads: list[threading.Thread],
    stop_event: threading.Event,
) -> tuple[bool, bool]:
    interrupted = False
    started: list[threading.Thread] = []

    try:
        for thread in threads:
            thread.start()
            started.append(thread)
    except KeyboardInterrupt:
        interrupted = True
        stop_event.set()

    while True:
        try:
            if not any(thread.is_alive() for thread in started):
                return interrupted, False
            for thread in started:
                thread.join(timeout=0.2)
        except KeyboardInterrupt:
            if interrupted:
                return True, True
            interrupted = True
            stop_event.set()


def _discover_connected_inputs(
    settings: InputSettings,
) -> tuple[_ConnectedInputs, bool]:
    discovery_errors = False
    gmail_connected = False
    discord_credentials: DiscordCredentials | None = None

    try:
        gmail = gmail_status()
    except GmailError as exc:
        write_terminal(f"Error: Gmail input cannot start: {exc}", file=sys.stderr)
        discovery_errors = True
    else:
        if not gmail.connected:
            write_terminal("- Gmail: not connected")
        elif not settings.is_enabled("gmail"):
            write_terminal("- Gmail: connected, paused")
        else:
            gmail_connected = True

    try:
        discord = discord_status()
    except DiscordError as exc:
        write_terminal(f"Error: Discord input cannot start: {exc}", file=sys.stderr)
        discovery_errors = True
    else:
        if not discord.connected:
            write_terminal("- Discord: not connected")
        elif not settings.is_enabled("discord"):
            write_terminal("- Discord: connected, paused")
        else:
            try:
                discord_credentials = load_discord_credentials()
            except DiscordError as exc:
                write_terminal(
                    f"Error: Discord input cannot start: {exc}",
                    file=sys.stderr,
                )
                discovery_errors = True

    return (
        _ConnectedInputs(
            gmail=gmail_connected,
            discord=discord_credentials,
        ),
        discovery_errors,
    )


def _build_workers(
    connected: _ConnectedInputs,
    pipeline: MessagePipeline,
    *,
    stop_event: threading.Event,
    interval: float,
    destination_name: str,
) -> tuple[_Worker, ...]:
    workers: list[_Worker] = []
    if connected.gmail:

        def watch_gmail() -> int:
            try:
                connector = GmailConnector(open_gmail_mailbox())
            except GmailError as exc:
                write_terminal(
                    f"Error: Gmail input cannot start: {exc}",
                    file=sys.stderr,
                )
                return 1
            write_terminal(f"✓ Gmail: watching every {interval:g} seconds")
            return connections_gmail.watch_connected_gmail(
                connector,
                pipeline,
                interval=interval,
                stop_event=stop_event,
            )

        workers.append(_Worker("Gmail", watch_gmail))

    if connected.discord is not None:
        credentials = connected.discord

        def watch_discord_input() -> int:
            return connections_discord.watch_connected_discord(
                DiscordConnector(credentials),
                pipeline,
                destination_name=destination_name,
                stop_event=stop_event,
                ready_message=(
                    f"✓ Discord: watching #{terminal_safe(credentials.channel_name)}"
                ),
                announce_stop=False,
            )

        workers.append(_Worker("Discord", watch_discord_input))

    return tuple(workers)


COMMAND = Command(
    name="watch",
    help="Watch every connected and enabled input.",
    handler=run,
    configure=configure,
)
