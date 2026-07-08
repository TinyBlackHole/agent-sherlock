import argparse
import json

import pytest

from agent_sherlock.cli import main
from agent_sherlock.commands import connections


class FakeListRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeMessages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def list(self, **_kwargs):
        response = self.pages[self.calls]
        self.calls += 1
        return FakeListRequest(response)


class FakeUsers:
    def __init__(self, pages):
        self._messages = FakeMessages(pages)

    def messages(self):
        return self._messages


class FakeGmailService:
    def __init__(self, pages):
        self._users = FakeUsers(pages)

    def users(self):
        return self._users


def test_connections_command_appears_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "connections" in capsys.readouterr().out


def test_connections_without_provider_prints_help(capsys):
    assert main(["connections"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections" in output
    assert "gmail" in output


def test_gmail_without_action_prints_help(capsys):
    assert main(["connections", "gmail"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections gmail" in output
    assert "connect" in output
    assert "watch" in output


def test_gmail_connect_missing_credentials_returns_nonzero(capsys):
    assert (
        main(
            [
                "connections",
                "gmail",
                "connect",
                "--credentials",
                "/does/not/exist.json",
            ]
        )
        == 2
    )

    assert "Gmail credentials file not found" in capsys.readouterr().out


def test_config_dir_uses_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(tmp_path))

    assert (
        connections.gmail_state_path()
        == tmp_path / "connections" / "gmail" / "state.json"
    )


def test_watch_once_saves_baseline_without_touch(monkeypatch, tmp_path, capsys):
    touched = []
    monkeypatch.setattr(connections, "touch_hello", lambda: touched.append(True))
    service = FakeGmailService([{"messages": [{"id": "old-1"}, {"id": "old-2"}]}])

    assert connections.gmail_watch_once(service, tmp_path / "state.json") == 0

    assert touched == []
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "seen_message_ids": ["old-1", "old-2"]
    }
    assert "baseline saved" in capsys.readouterr().out


def test_watch_once_touches_once_per_new_message(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"seen_message_ids": ["old-1"]}))
    touched = []
    monkeypatch.setattr(connections, "touch_hello", lambda: touched.append(True))
    service = FakeGmailService(
        [{"messages": [{"id": "old-1"}, {"id": "new-1"}, {"id": "new-2"}]}]
    )

    assert connections.gmail_watch_once(service, state) == 2

    assert touched == [True, True]
    assert json.loads(state.read_text()) == {
        "seen_message_ids": ["new-1", "new-2", "old-1"]
    }


def test_watch_once_reads_paginated_inbox(tmp_path):
    service = FakeGmailService(
        [
            {"messages": [{"id": "one"}], "nextPageToken": "page-2"},
            {"messages": [{"id": "two"}]},
        ]
    )

    assert connections.gmail_watch_once(service, tmp_path / "state.json") == 0
    assert json.loads((tmp_path / "state.json").read_text()) == {
        "seen_message_ids": ["one", "two"]
    }


def test_gmail_watch_missing_connection_returns_nonzero(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(tmp_path))

    assert connections.run_gmail_watch(argparse.Namespace(interval=30)) == 2
    assert "Gmail is not connected" in capsys.readouterr().out


def test_gmail_watch_rejects_nonpositive_interval(capsys):
    assert connections.run_gmail_watch(argparse.Namespace(interval=0)) == 2
    assert "--interval must be greater than 0" in capsys.readouterr().out
