import json
import os
import stat

import pytest

from agent_sherlock import storage


def test_config_root_uses_sherlock_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", "/ignored")

    assert storage.config_root() == tmp_path


def test_config_root_uses_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SHERLOCK_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert storage.config_root() == tmp_path / "agent-sherlock"


def test_atomic_write_json_is_private_and_valid(tmp_path):
    target = tmp_path / "private" / "state.json"

    storage.atomic_write_json(target, {"hello": "world"})

    assert json.loads(target.read_text()) == {"hello": "world"}
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_atomic_write_preserves_existing_config_root_and_hardens_children(
    monkeypatch,
    tmp_path,
):
    config = tmp_path / "config"
    config.mkdir(mode=0o755)
    config.chmod(0o755)
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(config))
    target = config / "connections" / "gmail" / "state.json"

    storage.atomic_write_json(target, {"history_id": "1"})

    if os.name == "posix":
        assert stat.S_IMODE(config.stat().st_mode) == 0o755
        assert stat.S_IMODE((config / "connections").stat().st_mode) == 0o700
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700


def test_config_root_allows_a_symlink_boundary(monkeypatch, tmp_path):
    actual_root = tmp_path / "dotfiles" / "sherlock"
    actual_root.mkdir(parents=True)
    linked_root = tmp_path / "configured-sherlock"
    linked_root.symlink_to(actual_root, target_is_directory=True)
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(linked_root))

    target = storage.config_root() / "connections" / "gmail" / "state.json"
    storage.atomic_write_json(target, {"history_id": "1"})

    assert storage.config_root() == actual_root.resolve()
    assert json.loads(
        (actual_root / "connections" / "gmail" / "state.json").read_text()
    ) == {"history_id": "1"}


def test_atomic_write_replaces_existing_file(tmp_path):
    target = tmp_path / "private" / "state.json"
    storage.atomic_write_json(target, {"version": 1})

    storage.atomic_write_json(target, {"version": 2})

    assert json.loads(target.read_text()) == {"version": 2}
    assert list(target.parent.glob(f".{target.name}.*")) == []


def test_read_private_json_hardens_existing_permissions(tmp_path):
    target = tmp_path / "state.json"
    target.write_text('{"history_id": "1"}')
    target.chmod(0o644)

    assert storage.read_json_object(target, private=True) == {"history_id": "1"}
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_read_private_json_refuses_symbolic_link(tmp_path):
    source = tmp_path / "source.json"
    source.write_text("{}")
    link = tmp_path / "token.json"
    link.symlink_to(source)

    with pytest.raises(storage.StorageError, match="symbolic link"):
        storage.read_json_object(link, private=True)


def test_atomic_write_refuses_symbolic_link_in_private_directory_tree(
    monkeypatch,
    tmp_path,
):
    config = tmp_path / "config"
    outside = tmp_path / "outside"
    config.mkdir()
    outside.mkdir()
    (config / "connections").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(config))

    with pytest.raises(storage.StorageError, match="symbolic link"):
        storage.atomic_write_json(
            config / "connections" / "gmail" / "state.json",
            {"history_id": "1"},
        )

    assert not (outside / "gmail" / "state.json").exists()


def test_read_json_rejects_non_object(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("[]")

    with pytest.raises(storage.StorageError, match="JSON object"):
        storage.read_json_object(target)
