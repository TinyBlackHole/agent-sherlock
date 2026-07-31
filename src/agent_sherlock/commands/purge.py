from __future__ import annotations

import argparse
import sys

from agent_sherlock.commands.base import Command
from agent_sherlock.commands.connections_shared import print_error, write_terminal
from agent_sherlock.persistence import (
    DEFAULT_DEAD_LETTER_RETENTION_DAYS,
    DEFAULT_DELIVERED_RETENTION_DAYS,
    DEFAULT_MAX_STORED_MESSAGES,
    MessageCounts,
    MessageRepository,
    PersistenceError,
    RetentionPolicy,
)


def configure(parser: argparse.ArgumentParser) -> None:
    """Declare this command's arguments."""
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DELIVERED_RETENTION_DAYS,
        help=(
            "Delete delivered messages older than this many days. "
            f"Default: {DEFAULT_DELIVERED_RETENTION_DAYS}."
        ),
    )
    parser.add_argument(
        "--dead-letter-days",
        type=int,
        default=DEFAULT_DEAD_LETTER_RETENTION_DAYS,
        help=(
            "Delete dead-letter messages older than this many days. "
            f"Default: {DEFAULT_DEAD_LETTER_RETENTION_DAYS}."
        ),
    )
    parser.add_argument(
        "--max-messages",
        type=int,
        default=DEFAULT_MAX_STORED_MESSAGES,
        help=(
            "Trim the oldest handled messages once the inbox exceeds this many "
            f"rows. Default: {DEFAULT_MAX_STORED_MESSAGES}. Use 0 to disable."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Delete every delivered and dead-letter message, whatever its age.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Only report what the local inbox holds; delete nothing.",
    )


def run(args: argparse.Namespace) -> int:
    """Apply the retention policy to Sherlock's local message inbox."""
    days = getattr(args, "days", DEFAULT_DELIVERED_RETENTION_DAYS)
    dead_letter_days = getattr(
        args,
        "dead_letter_days",
        DEFAULT_DEAD_LETTER_RETENTION_DAYS,
    )
    max_messages = getattr(args, "max_messages", DEFAULT_MAX_STORED_MESSAGES)
    if getattr(args, "all", False):
        days = 0
        dead_letter_days = 0
    if days < 0 or dead_letter_days < 0 or max_messages < 0:
        write_terminal(
            "Error: retention days and message limits cannot be negative.",
            file=sys.stderr,
        )
        return 2

    repository = MessageRepository()
    try:
        if getattr(args, "status", False):
            _print_counts(repository.counts())
            return 0
        result = repository.apply_retention(
            RetentionPolicy(
                delivered_days=days,
                dead_letter_days=dead_letter_days,
                max_messages=max_messages,
            )
        )
        counts = repository.counts()
    except PersistenceError as exc:
        print_error(exc)
        return 1

    if not result.total:
        write_terminal("Nothing to purge; every stored message is within retention.")
    else:
        write_terminal(
            f"Purged {result.total} stored {_plural(result.total)}: "
            f"{result.delivered_removed} delivered, "
            f"{result.dead_letters_removed} dead-letter, "
            f"{result.over_limit_removed} over the size limit."
        )
    _print_counts(counts)
    return 0


def _print_counts(counts: MessageCounts) -> None:
    write_terminal(
        f"Local inbox: {counts.total} stored {_plural(counts.total)} "
        f"({counts.pending} pending, {counts.in_flight} in flight, "
        f"{counts.delivered} delivered, {counts.dead_letter} dead-letter) "
        f"using {_readable_size(counts.database_bytes)}."
    )


def _plural(count: int) -> str:
    return "message" if count == 1 else "messages"


def _readable_size(size_in_bytes: int) -> str:
    size = float(size_in_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


COMMAND = Command(
    name="purge",
    help="Delete old messages from Sherlock's local inbox.",
    handler=run,
    configure=configure,
)
