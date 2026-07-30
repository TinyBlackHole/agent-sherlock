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


def test_read_json_rejects_non_object(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("[]")

    with pytest.raises(storage.StorageError, match="JSON object"):
        storage.read_json_object(target)
