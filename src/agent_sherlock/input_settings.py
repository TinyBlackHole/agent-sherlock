from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

INPUT_NAMES = ("gmail", "discord")
INPUT_SETTINGS_SCHEMA_VERSION = 1


class InputSettingsError(RuntimeError):
    """Raised when automatic input-watch settings cannot be read or saved."""


@dataclass(frozen=True, slots=True)
class InputSettings:
    """Persistent opt-outs for inputs that otherwise watch automatically."""

    disabled: frozenset[str] = frozenset()

    def is_enabled(self, name: str) -> bool:
        _validate_input_name(name)
        return name not in self.disabled


def input_settings_path() -> Path:
    return config_root() / "inputs.json"


def load_input_settings(*, path: Path | None = None) -> InputSettings:
    selected_path = path or input_settings_path()
    try:
        data = read_json_object(
            selected_path,
            missing_ok=True,
            private=True,
        )
    except StorageError as exc:
        raise InputSettingsError(str(exc)) from exc
    if not data:
        return InputSettings()

    if data.get("schema_version") != INPUT_SETTINGS_SCHEMA_VERSION:
        raise InputSettingsError(
            "The input settings use an unsupported format. "
            "Remove inputs.json and configure them again."
        )
    disabled = data.get("disabled")
    if not isinstance(disabled, list) or any(
        not isinstance(name, str) or name not in INPUT_NAMES for name in disabled
    ):
        raise InputSettingsError(
            "The input settings are invalid. "
            "Remove inputs.json and configure them again."
        )
    return InputSettings(disabled=frozenset(disabled))


def input_is_enabled(name: str, *, path: Path | None = None) -> bool:
    return load_input_settings(path=path).is_enabled(name)


def set_input_enabled(
    name: str,
    enabled: bool,
    *,
    path: Path | None = None,
) -> None:
    _validate_input_name(name)
    selected_path = path or input_settings_path()
    settings = load_input_settings(path=selected_path)
    disabled = set(settings.disabled)
    if enabled:
        disabled.discard(name)
    else:
        disabled.add(name)
    try:
        atomic_write_json(
            selected_path,
            {
                "disabled": sorted(disabled),
                "schema_version": INPUT_SETTINGS_SCHEMA_VERSION,
            },
        )
    except StorageError as exc:
        raise InputSettingsError(
            f"Cannot save automatic input-watch settings: {selected_path}"
        ) from exc


def _validate_input_name(name: str) -> None:
    if name not in INPUT_NAMES:
        choices = ", ".join(INPUT_NAMES)
        raise InputSettingsError(f"Unknown input: {name}. Choose one of: {choices}.")
