from __future__ import annotations

import sys

MAX_TERMINAL_FIELD_CHARACTERS = 1_000


def is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def read_menu_choice() -> str:
    try:
        return input("Select an option: ").strip().casefold()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def print_error(error: Exception) -> None:
    print(f"Error: {error}", file=sys.stderr)


def terminal_safe(value: str, *, fallback: str = "(unknown)") -> str:
    """Collapse untrusted provider text and strip terminal control characters."""
    cleaned = "".join(
        character if character.isprintable() else " " for character in value
    )
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_TERMINAL_FIELD_CHARACTERS:
        cleaned = f"{cleaned[: MAX_TERMINAL_FIELD_CHARACTERS - 1]}…"
    return cleaned or fallback
