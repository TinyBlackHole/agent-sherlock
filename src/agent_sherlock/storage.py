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
        root = Path(override).expanduser()
    else:
        xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config_home:
            root = Path(xdg_config_home).expanduser() / "agent-sherlock"
        else:
            root = Path.home() / ".config" / "agent-sherlock"

    # Resolving only the configured boundary permits legitimate dotfile-managed
    # roots while symlinks created inside that boundary remain detectable.
    return root.resolve(strict=False)


def ensure_private_directory(
    path: Path,
    *,
    preserve_existing_mode: bool = False,
) -> None:
    path = path.expanduser().absolute()
    if path == path.parent:
        raise StorageError(
            f"Refusing to use a filesystem root as private configuration: {path}"
        )

    path_existed = path.exists()
    missing_directories: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing_directories.append(current)
        current = current.parent

    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        directories_to_check = [*missing_directories, path]
        for directory in directories_to_check:
            directory_stat = directory.lstat()
            if stat.S_ISLNK(directory_stat.st_mode):
                raise StorageError(
                    f"Refusing to use a symbolic link as a private directory: "
                    f"{directory}"
                )
            if not stat.S_ISDIR(directory_stat.st_mode):
                raise StorageError(
                    f"Configuration path is not a directory: {directory}"
                )
        if os.name == "posix":
            directories_to_harden = missing_directories
            if not (preserve_existing_mode and path_existed):
                directories_to_harden = [*directories_to_harden, path]
            for directory in dict.fromkeys(directories_to_harden):
                directory.chmod(PRIVATE_DIRECTORY_MODE)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(
            f"Cannot create private configuration directory: {path}"
        ) from exc


def harden_private_file(path: Path) -> None:
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
        harden_private_file(path)

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
    configuration_root = config_root().absolute()
    absolute_path = path.absolute()
    if absolute_path != configuration_root and absolute_path.is_relative_to(
        configuration_root
    ):
        ensure_private_directory(
            configuration_root,
            preserve_existing_mode=True,
        )
        relative_parent = absolute_path.parent.relative_to(configuration_root)
        private_parent = configuration_root
        for part in relative_parent.parts:
            private_parent /= part
            ensure_private_directory(private_parent)
    else:
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
