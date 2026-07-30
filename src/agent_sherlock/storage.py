from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class StorageError(RuntimeError):
    """Raised when Sherlock cannot safely read or write local state."""


def config_root() -> Path:
    """Return the platform-appropriate Sherlock configuration directory."""
    override = os.environ.get("SHERLOCK_CONFIG_DIR")
    if override:
        return Path(override).expanduser()

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home).expanduser() / "agent-sherlock"

    return Path.home() / ".config" / "agent-sherlock"


def ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        if os.name == "posix":
            path.chmod(PRIVATE_DIRECTORY_MODE)
    except OSError as exc:
        raise StorageError(
            f"Cannot create private configuration directory: {path}"
        ) from exc


def _harden_private_file(path: Path) -> None:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise StorageError(f"Cannot inspect configuration file: {path}") from exc

    if stat.S_ISLNK(file_stat.st_mode):
        raise StorageError(f"Refusing to use a symbolic link as a private file: {path}")
    if not stat.S_ISREG(file_stat.st_mode):
        raise StorageError(f"Configuration path is not a regular file: {path}")

    if os.name == "posix":
        try:
            path.chmod(PRIVATE_FILE_MODE)
        except OSError as exc:
            raise StorageError(f"Cannot secure configuration file: {path}") from exc


def read_json_object(
    path: Path,
    *,
    missing_ok: bool = False,
    private: bool = False,
) -> dict[str, Any]:
    if not path.exists():
        if missing_ok:
            return {}
        raise StorageError(f"Configuration file not found: {path}")

    if private:
        _harden_private_file(path)

    try:
        with path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError(f"Cannot read valid JSON from: {path}") from exc

    if not isinstance(data, dict):
        raise StorageError(f"Expected a JSON object in: {path}")
    return data


def atomic_write_text(
    path: Path,
    content: str,
    *,
    mode: int = PRIVATE_FILE_MODE,
) -> None:
    """Write a file privately and atomically in its destination directory."""
    ensure_private_directory(path.parent)
    temporary_path: Path | None = None

    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            text=True,
        )
        temporary_path = Path(temporary_name)
        if os.name == "posix":
            os.fchmod(descriptor, mode)

        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
        if os.name == "posix":
            path.chmod(mode)
    except OSError as exc:
        raise StorageError(f"Cannot securely write configuration file: {path}") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    serialized = json.dumps(data, indent=2, sort_keys=True)
    atomic_write_text(path, f"{serialized}\n")
