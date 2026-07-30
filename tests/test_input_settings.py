import json
import os
import stat

import pytest

from agent_sherlock import input_settings


def test_connected_inputs_are_enabled_by_default(tmp_path):
    path = tmp_path / "inputs.json"

    settings = input_settings.load_input_settings(path=path)

    assert settings.is_enabled("gmail") is True
    assert settings.is_enabled("discord") is True
    assert not path.exists()


def test_an_input_can_be_paused_and_reenabled(tmp_path):
    path = tmp_path / "private" / "inputs.json"

    input_settings.set_input_enabled("gmail", False, path=path)

    assert input_settings.input_is_enabled("gmail", path=path) is False
    assert input_settings.input_is_enabled("discord", path=path) is True
    assert json.loads(path.read_text()) == {
        "disabled": ["gmail"],
        "schema_version": 1,
    }
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    input_settings.set_input_enabled("gmail", True, path=path)

    assert input_settings.input_is_enabled("gmail", path=path) is True


@pytest.mark.parametrize(
    "data",
    [
        {"disabled": ["carrier-pigeon"], "schema_version": 1},
        {"disabled": "gmail", "schema_version": 1},
        {"disabled": [], "schema_version": 999},
    ],
)
def test_invalid_input_settings_are_rejected(tmp_path, data):
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(data))

    with pytest.raises(input_settings.InputSettingsError):
        input_settings.load_input_settings(path=path)


def test_unknown_input_names_are_rejected(tmp_path):
    with pytest.raises(input_settings.InputSettingsError, match="Unknown input"):
        input_settings.set_input_enabled(
            "carrier-pigeon",
            False,
            path=tmp_path / "inputs.json",
        )
