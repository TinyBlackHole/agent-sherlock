from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from agent_sherlock.commands import output_telegram
from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections_shared import (
    is_interactive_terminal,
    print_error,
    read_menu_choice,
    terminal_safe,
)
from agent_sherlock.destinations.active import (
    DESTINATION_NAMES,
    DestinationSelectionError,
    active_destination_name,
    open_active_destination,
    set_active_destination,
)
from agent_sherlock.integrations.discord_webhook import (
    DiscordWebhookError,
    DiscordWebhookStatus,
    connect_discord_webhook,
    discord_webhook_status,
)
from agent_sherlock.integrations.telegram import TelegramError, telegram_status

MAX_WEBHOOK_FILE_BYTES = 4_096
TEST_MESSAGE = "Agent Sherlock output test successful."


def configure(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(output_parser=parser)
    actions = parser.add_subparsers(dest="action", metavar="<action>")

    use = actions.add_parser(
        "use",
        help="Choose where Sherlock delivers every message.",
        description="Choose where Sherlock delivers every message.",
    )
    use.add_argument(
        "destination",
        nargs="?",
        metavar="{telegram,discord}",
        help="The destination that receives all forwarded messages.",
    )
    use.set_defaults(output_handler=run_use)

    status = actions.add_parser(
        "status",
        help="Show the active output destination and both connections.",
        description="Show the active output destination and both connections.",
    )
    status.set_defaults(output_handler=run_status)

    test = actions.add_parser(
        "test",
        help="Send a test message to the active output destination.",
        description="Send a test message to the active output destination.",
    )
    test.set_defaults(output_handler=run_test)

    output_telegram.configure(actions)

    discord = actions.add_parser(
        "discord",
        help="Manage the Discord webhook output.",
        description="Manage the Discord webhook output.",
    )
    discord_actions = discord.add_subparsers(dest="discord_action", metavar="<action>")
    connect = discord_actions.add_parser(
        "connect",
        help="Connect a Discord channel webhook as Sherlock's output.",
        description=(
            "Connect a Discord channel webhook as Sherlock's output. Create the "
            "webhook in Channel Settings -> Integrations -> Webhooks."
        ),
    )
    connect.add_argument(
        "--webhook-url-file",
        help="File containing the Discord webhook URL.",
    )
    connect.set_defaults(output_handler=run_discord_connect)
    discord.set_defaults(output_parser=discord)


def run(args: argparse.Namespace) -> int:
    handler = getattr(args, "output_handler", None)
    if handler is not None:
        return handler(args)

    if is_interactive_terminal():
        output_menu = getattr(args, "output_menu", None)
        if output_menu is not None:
            return output_menu()
        return run_menu()

    parser = getattr(args, "output_parser", None)
    if parser is not None:
        parser.print_help()
    return 0


def run_menu() -> int:
    print("Agent Sherlock output")
    print("  1. Manage Telegram output")
    print("  2. Deliver to Telegram")
    print("  3. Deliver to Discord")
    print("  4. Connect Discord webhook")
    print("  5. Show output status")
    print("  6. Send test message")
    print("  q. Quit")

    handlers = {
        "1": output_telegram.run_menu,
        "2": lambda: run_use(argparse.Namespace(destination="telegram")),
        "3": lambda: run_use(argparse.Namespace(destination="discord")),
        "4": lambda: run_discord_connect(
            argparse.Namespace(webhook_url_file=None),
        ),
        "5": lambda: run_status(argparse.Namespace()),
        "6": lambda: run_test(argparse.Namespace()),
    }
    while True:
        choice = read_menu_choice()
        if choice in {"q", "quit", "exit"}:
            return 0
        handler = handlers.get(choice)
        if handler is not None:
            return handler()
        print("Choose 1, 2, 3, 4, 5, 6, or q.")


def _webhook_url_from_args(args: argparse.Namespace) -> str | None:
    webhook_url_file = getattr(args, "webhook_url_file", None)
    if webhook_url_file:
        path = Path(webhook_url_file).expanduser()
        try:
            if not path.is_file() or path.stat().st_size > MAX_WEBHOOK_FILE_BYTES:
                raise OSError
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            print(
                f"Error: cannot read Discord webhook URL file: {path}",
                file=sys.stderr,
            )
            return None
    if not is_interactive_terminal():
        return None
    try:
        return getpass.getpass("Discord webhook URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None


def run_discord_connect(args: argparse.Namespace) -> int:
    url = _webhook_url_from_args(args)
    if not url:
        print(
            "Error: enter a webhook URL interactively or provide --webhook-url-file.",
            file=sys.stderr,
        )
        return 2
    try:
        status = connect_discord_webhook(url)
    except DiscordWebhookError as exc:
        print_error(exc)
        return 1

    print(
        f"Discord output connected: {terminal_safe(status.name)} "
        f"in channel {status.channel_id}."
    )
    if active_destination_name_or_none() != "discord":
        print("Run `sherlock output use discord` to start delivering there.")
    return 0


def active_destination_name_or_none() -> str | None:
    try:
        return active_destination_name()
    except DestinationSelectionError:
        return None


def run_use(args: argparse.Namespace) -> int:
    destination = getattr(args, "destination", "")
    if destination not in DESTINATION_NAMES:
        print(
            "Error: choose an output destination: telegram or discord.",
            file=sys.stderr,
        )
        return 2
    try:
        set_active_destination(destination)
    except (DestinationSelectionError, TelegramError, DiscordWebhookError) as exc:
        print_error(exc)
        return 1
    print(f"Sherlock now delivers every message to {destination}.")
    return 0


def _discord_status_line(status: DiscordWebhookStatus) -> str:
    if not status.connected:
        return "  discord: not connected"
    return (
        f"  discord: connected ({terminal_safe(status.name)} "
        f"in channel {status.channel_id})"
    )


def run_status(_args: argparse.Namespace) -> int:
    try:
        active = active_destination_name()
    except DestinationSelectionError as exc:
        print_error(exc)
        return 1

    print(f"Active output destination: {active}")
    try:
        telegram = telegram_status()
    except TelegramError as exc:
        print_error(exc)
        return 1
    if telegram.connected:
        print(f"  telegram: connected (@{terminal_safe(telegram.bot_username)})")
    else:
        print("  telegram: not connected")

    try:
        discord = discord_webhook_status()
    except DiscordWebhookError as exc:
        print_error(exc)
        return 1
    print(_discord_status_line(discord))
    return 0


def run_test(_args: argparse.Namespace) -> int:
    try:
        destination = open_active_destination()
        destination.send(TEST_MESSAGE)
    except (DestinationSelectionError, TelegramError, DiscordWebhookError) as exc:
        print_error(exc)
        return 1
    print(f"Test message sent to {destination.name}.")
    return 0


COMMAND = Command(
    name="output",
    help="Choose and connect where Sherlock delivers messages.",
    handler=run,
    configure=configure,
)
