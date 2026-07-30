from __future__ import annotations

import sys
import threading
from collections.abc import Callable
from typing import TextIO

from agent_sherlock.input_settings import InputSettingsError, set_input_enabled

MAX_TERMINAL_FIELD_CHARACTERS = 1_000
_OUTPUT_LOCK = threading.Lock()


def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_menu_choice() -> str:
    try:
        return input("Select an option: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def print_error(error: Exception) -> None:
    write_terminal(f"Error: {error}", file=sys.stderr)


def write_terminal(message: str, *, file: TextIO | None = None) -> None:
    """Write one complete line without interleaving concurrent watchers."""
    stream = sys.stdout if file is None else file
    with _OUTPUT_LOCK:
        stream.write(f"{message}\n")


def automatic_watch_status(*, connected: bool, enabled: bool) -> str:
    if enabled:
        suffix = "" if connected else " after connection"
        return f"Automatic watch: enabled{suffix}."
    return "Automatic watch: paused."


def enable_automatic_watch(
    input_name: str,
    display_name: str,
    *,
    is_connected: Callable[[], bool],
    provider_errors: tuple[type[Exception], ...],
) -> int:
    try:
        if not is_connected():
            write_terminal(
                f"Error: connect {display_name} before enabling automatic watching.",
                file=sys.stderr,
            )
            return 1
        set_input_enabled(input_name, True)
    except provider_errors + (InputSettingsError,) as exc:
        print_error(exc)
        return 1
    write_terminal(f"{display_name} automatic watching enabled.")
    return 0


def disable_automatic_watch(input_name: str, display_name: str) -> int:
    try:
        set_input_enabled(input_name, False)
    except InputSettingsError as exc:
        print_error(exc)
        return 1
    write_terminal(f"{display_name} remains connected; automatic watching is paused.")
    return 0


def terminal_safe(value: str, *, fallback: str = "(unknown)") -> str:
    """Collapse untrusted provider text and strip terminal control characters."""
    cleaned = "".join(
        character if character.isprintable() else " " for character in value
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_TERMINAL_FIELD_CHARACTERS:
        cleaned = f"{cleaned[: MAX_TERMINAL_FIELD_CHARACTERS - 1]}…"
    return cleaned or fallback
