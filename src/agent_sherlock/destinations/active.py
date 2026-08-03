from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from agent_sherlock.destinations.base import MessageDestination
from agent_sherlock.destinations.discord import DiscordDestination
from agent_sherlock.destinations.telegram import TelegramDestination
from agent_sherlock.integrations.discord_webhook import (
    DiscordWebhookAPIError,
    DiscordWebhookError,
)
from agent_sherlock.integrations.telegram import TelegramAPIError, TelegramError
from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

DESTINATION_NAMES = ("telegram", "discord")
# Telegram was Sherlock's only output before the selection existed, so an
# unwritten preference keeps existing installations delivering where they did.
DEFAULT_DESTINATION_NAME = "telegram"


class DestinationSelectionError(RuntimeError):
    """Raised when the configured output destination cannot be resolved."""


# Callers handle delivery failures without knowing which destination is active,
# so both providers' error families are caught together.
DESTINATION_ERRORS: tuple[type[Exception], ...] = (
    DestinationSelectionError,
    TelegramError,
    DiscordWebhookError,
)
DESTINATION_API_ERRORS: tuple[type[Exception], ...] = (
    TelegramAPIError,
    DiscordWebhookAPIError,
)
DestinationValidator = Callable[[MessageDestination], None]


@dataclass(frozen=True, slots=True)
class DestinationPaths:
    directory: Path

    @classmethod
    def default(cls) -> DestinationPaths:
        return cls(config_root())

    @property
    def selection(self) -> Path:
        return self.directory / "destination.json"


class ActiveDestination:
    """Resolve the selected destination immediately before every delivery."""

    def __init__(
        self,
        *,
        paths: DestinationPaths | None = None,
        validator: DestinationValidator | None = None,
    ):
        self.paths = paths
        self.validator = validator

    @classmethod
    def open(
        cls,
        *,
        paths: DestinationPaths | None = None,
        validator: DestinationValidator | None = None,
    ) -> ActiveDestination:
        destination = cls(paths=paths, validator=validator)
        # Fail at startup if the current selection is unusable. Later calls to
        # send resolve again so a running watcher observes output changes.
        destination._resolve()
        return destination

    @property
    def name(self) -> str:
        return active_destination_name(paths=self.paths)

    def send(self, text: str) -> None:
        self._resolve().send(text)

    def send_important(self, text: str, *, discord_user_id: str) -> None:
        self._resolve().send_important(text, discord_user_id=discord_user_id)

    def _resolve(self) -> MessageDestination:
        destination = open_active_destination(paths=self.paths)
        if self.validator is not None:
            self.validator(destination)
        return destination


def active_destination_name(*, paths: DestinationPaths | None = None) -> str:
    selected_paths = paths or DestinationPaths.default()
    if not selected_paths.selection.exists():
        return DEFAULT_DESTINATION_NAME
    try:
        data = read_json_object(selected_paths.selection)
    except StorageError as exc:
        raise DestinationSelectionError(str(exc)) from exc

    name = data.get("destination")
    if name not in DESTINATION_NAMES:
        raise DestinationSelectionError(
            "The stored output destination is invalid. Run "
            "`sherlock output use <telegram|discord>`."
        )
    return name


def set_active_destination(
    name: str,
    *,
    paths: DestinationPaths | None = None,
) -> str:
    selected_paths = paths or DestinationPaths.default()
    if name not in DESTINATION_NAMES:
        raise DestinationSelectionError(
            f"Unknown output destination: {name}. "
            f"Choose one of: {', '.join(DESTINATION_NAMES)}."
        )
    # Resolving now surfaces a missing connection while the user is still at the
    # prompt, instead of at the first delivery attempt.
    open_destination(name)
    try:
        atomic_write_json(selected_paths.selection, {"destination": name})
    except StorageError as exc:
        raise DestinationSelectionError(
            "Could not save the output destination selection."
        ) from exc
    return name


def open_destination(name: str) -> MessageDestination:
    if name == "telegram":
        return TelegramDestination.open()
    if name == "discord":
        return DiscordDestination.open()
    raise DestinationSelectionError(f"Unknown output destination: {name}.")


def open_active_destination(
    *,
    paths: DestinationPaths | None = None,
) -> MessageDestination:
    """Open the destination every input connector delivers to."""
    return open_destination(active_destination_name(paths=paths))
