import argparse
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_sherlock import input_settings
from agent_sherlock.application import MessagePipeline, SyncResult
from agent_sherlock.cli import main
from agent_sherlock.commands import (
    connections,
    connections_discord,
    connections_gmail,
    connections_shared,
)
from agent_sherlock.connectors import ConnectorBatch
from agent_sherlock.domain import InboundMessage
from agent_sherlock.integrations.discord import DiscordStatus
from agent_sherlock.integrations.gmail import (
    GmailAuthenticationError,
    GmailProfile,
    GmailStatus,
)
from agent_sherlock.integrations.telegram import TelegramAPIError
from agent_sherlock.persistence import MessageRepository


def test_connections_command_appears_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "connections" in capsys.readouterr().out


def test_connections_without_provider_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(connections, "is_interactive_terminal", lambda: False)

    assert main(["connections"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections" in output
    assert "gmail" in output
    assert "discord" in output
    assert "telegram" not in output


def test_gmail_without_action_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(connections, "is_interactive_terminal", lambda: False)

    assert main(["connections", "gmail"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections gmail" in output
    assert "connect" in output
    assert "fetch" in output
    assert "watch" in output
    assert "status" in output
    assert "enable" in output
    assert "disable" in output


def test_gmail_without_action_opens_gmail_menu_in_terminal(monkeypatch):
    monkeypatch.setattr(connections, "is_interactive_terminal", lambda: True)
    monkeypatch.setattr(connections_gmail, "run_menu", lambda: 17)

    def unexpected_root_menu():
        raise AssertionError("The root connections menu should not open.")

    monkeypatch.setattr(connections, "run_interactive_menu", unexpected_root_menu)

    assert main(["connections", "gmail"]) == 17


def test_gmail_connect_requires_credentials_without_terminal(monkeypatch, capsys):
    monkeypatch.setattr(
        connections_gmail,
        "is_interactive_terminal",
        lambda: False,
    )

    assert main(["connections", "gmail", "connect"]) == 2

    assert "provide --credentials" in capsys.readouterr().err


def test_gmail_connect_prints_connected_account(monkeypatch, capsys):
    seen = []
    input_settings.set_input_enabled("gmail", False)

    def fake_connect(path):
        seen.append(path)
        return GmailProfile(
            email_address="person@example.com",
            history_id="123",
        )

    monkeypatch.setattr(connections_gmail, "connect_gmail", fake_connect)

    assert (
        main(
            [
                "connections",
                "gmail",
                "connect",
                "--credentials",
                "/tmp/client.json",
            ]
        )
        == 0
    )

    assert seen == [Path("/tmp/client.json")]
    assert input_settings.input_is_enabled("gmail") is True
    output = capsys.readouterr().out
    assert "Gmail connected: person@example.com" in output
    assert "cannot modify or send email" in output


def test_gmail_connect_reports_success_before_auto_enable_warning(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        connections_gmail,
        "connect_gmail",
        lambda _path: GmailProfile(
            email_address="person@example.com",
            history_id="123",
        ),
    )
    monkeypatch.setattr(
        connections_gmail,
        "set_input_enabled",
        lambda *_args: (_ for _ in ()).throw(
            input_settings.InputSettingsError("settings unavailable")
        ),
    )

    assert (
        main(
            [
                "connections",
                "gmail",
                "connect",
                "--credentials",
                "/tmp/client.json",
            ]
        )
        == 1
    )

    output = capsys.readouterr()
    assert "Gmail connected: person@example.com" in output.out
    assert "Warning: Gmail is connected" in output.err


def test_gmail_fetch_missing_connection_returns_nonzero(monkeypatch, capsys):
    def fail_to_open():
        raise GmailAuthenticationError("Gmail is not connected.")

    monkeypatch.setattr(connections_gmail, "_open_pipeline", fail_to_open)

    assert main(["connections", "gmail", "fetch"]) == 1
    assert "Gmail is not connected" in capsys.readouterr().err


def test_gmail_fetch_delivers_only_through_pipeline(monkeypatch, capsys):
    class Pipeline:
        def sync(self, connector):
            assert connector == "gmail-connector"
            return SyncResult(discovered=1, stored=1, delivered=1)

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: ("gmail-connector", Pipeline()),
    )

    assert main(["connections", "gmail", "fetch"]) == 0

    output = capsys.readouterr().out
    assert output == "Sent 1 message to the configured output.\n"


def test_gmail_fetch_json_reports_pipeline_counts(monkeypatch, capsys):
    class Pipeline:
        def sync(self, _connector):
            return SyncResult(discovered=2, stored=2, delivered=2)

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (object(), Pipeline()),
    )

    assert main(["connections", "gmail", "fetch", "--json"]) == 0

    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed == {
        "dead_lettered": 0,
        "delivered": 2,
        "discovered": 2,
        "history_reset": False,
        "initialized": False,
        "stored": 2,
        "truncated": 0,
    }


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
def test_gmail_watch_rejects_invalid_interval(interval, capsys):
    assert (
        connections_gmail.run_watch(argparse.Namespace(interval=interval, json=False))
        == 2
    )
    assert (
        "--interval must be a finite number greater than 0" in capsys.readouterr().err
    )


def test_gmail_watch_stops_cleanly(monkeypatch, capsys):
    class Pipeline:
        def sync(self, _connector):
            return SyncResult()

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (object(), Pipeline()),
    )

    def stop(_delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(connections_gmail.time, "sleep", stop)

    assert connections_gmail.run_watch(argparse.Namespace(interval=30, json=False)) == 0
    assert "Stopped Gmail watch" in capsys.readouterr().out


def test_gmail_watch_does_not_retry_nonretryable_api_error(monkeypatch, capsys):
    class Pipeline:
        def sync(self, _connector):
            raise connections_gmail.GmailAPIError("Access denied.", status=403)

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (object(), Pipeline()),
    )

    assert connections_gmail.run_watch(argparse.Namespace(interval=30, json=False)) == 1
    assert "Access denied" in capsys.readouterr().err


def test_gmail_watch_retries_rate_limit_with_backoff(monkeypatch, capsys):
    class Pipeline:
        def sync(self, _connector):
            raise connections_gmail.GmailAPIError(
                "Rate limited.",
                status=403,
                reasons=frozenset({"userRateLimitExceeded"}),
            )

    delays = []

    def stop_after_delay(delay):
        delays.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (object(), Pipeline()),
    )
    monkeypatch.setattr(connections_gmail.time, "sleep", stop_after_delay)

    assert connections_gmail.run_watch(argparse.Namespace(interval=30, json=False)) == 0
    assert delays == [30]
    assert "Retrying in 30 seconds" in capsys.readouterr().err


def test_gmail_watch_caps_provider_retry_after(monkeypatch, capsys):
    class Pipeline:
        def sync(self, _connector):
            raise TelegramAPIError(
                "Flood control.",
                status=429,
                retry_after=10_000,
            )

    delays = []

    def stop_after_delay(delay):
        delays.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (object(), Pipeline()),
    )
    monkeypatch.setattr(connections_gmail.time, "sleep", stop_after_delay)

    assert connections_gmail.run_watch(argparse.Namespace(interval=30, json=False)) == 0
    assert delays == [connections_gmail.MAX_RETRY_DELAY_SECONDS]
    assert "Retrying in 300 seconds" in capsys.readouterr().err


def test_gmail_watch_dead_letters_permanent_delivery_error_without_exiting(
    monkeypatch,
    tmp_path,
    capsys,
):
    message = InboundMessage(
        source="gmail",
        account_id="person@example.com",
        external_id="poison",
        conversation_id="thread-1",
        sender="sender@example.com",
        subject="Hello",
        body="Message body",
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
    )
    batch = ConnectorBatch(messages=(message,), checkpoint="120")

    class Connector:
        name = "gmail"

        def poll(self):
            return batch

        def acknowledge(self, _batch):
            pass

    class PermanentFailureDestination:
        name = "telegram"

        def send(self, _text):
            raise TelegramAPIError(
                "Bad Request: chat not found",
                status=400,
            )

    repository = MessageRepository(tmp_path / "sherlock.db")
    pipeline = MessagePipeline(repository, PermanentFailureDestination())
    delays = []

    def stop_after_dead_letter(delay):
        delays.append(delay)
        if len(delays) == 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(
        connections_gmail,
        "_open_pipeline",
        lambda: (Connector(), pipeline),
    )
    monkeypatch.setattr(connections_gmail.time, "sleep", stop_after_dead_letter)

    assert connections_gmail.run_watch(argparse.Namespace(interval=10, json=False)) == 0
    assert delays == [10, 20, 10]
    assert repository.dead_letter_count() == 1
    output = capsys.readouterr()
    assert "Delivery remains queued; retrying in 10 seconds" in output.err
    assert "dead-letter queue" in output.err


def test_gmail_status_reports_dead_letter_count(monkeypatch, capsys):
    class Repository:
        def dead_letter_count(self):
            return 2

    monkeypatch.setattr(
        connections_gmail,
        "gmail_status",
        lambda: GmailStatus(connected=True, email_address="person@example.com"),
    )
    monkeypatch.setattr(connections_gmail, "MessageRepository", Repository)

    assert main(["connections", "gmail", "status"]) == 0
    assert capsys.readouterr().out == (
        "Gmail is connected: person@example.com\n"
        "Automatic watch: enabled.\n"
        "Dead-letter queue: 2 messages.\n"
    )


def test_gmail_can_be_paused_and_enabled_without_reconnecting(
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        connections_gmail,
        "gmail_status",
        lambda: GmailStatus(connected=True, email_address="person@example.com"),
    )

    assert main(["connections", "gmail", "disable"]) == 0
    assert input_settings.input_is_enabled("gmail") is False
    assert "remains connected" in capsys.readouterr().out

    assert main(["connections", "gmail", "enable"]) == 0
    assert input_settings.input_is_enabled("gmail") is True
    assert "automatic watching enabled" in capsys.readouterr().out


def test_gmail_cannot_be_enabled_before_it_is_connected(monkeypatch, capsys):
    monkeypatch.setattr(
        connections_gmail,
        "gmail_status",
        lambda: GmailStatus(connected=False),
    )

    assert main(["connections", "gmail", "enable"]) == 1
    assert "connect Gmail" in capsys.readouterr().err


def test_interactive_menu_dispatches_gmail_selection(monkeypatch, capsys):
    choices = iter(["1", "4"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))
    monkeypatch.setattr(
        connections_gmail,
        "gmail_status",
        lambda: GmailStatus(connected=True, email_address="person@example.com"),
    )
    monkeypatch.setattr(
        connections_gmail,
        "MessageRepository",
        lambda: type("Repository", (), {"dead_letter_count": lambda self: 0})(),
    )

    assert connections.run_interactive_menu() == 0

    output = capsys.readouterr().out
    assert "1. Gmail" in output
    assert "2. Discord" in output
    assert "3. Watch all active inputs" in output
    assert "Telegram" not in output
    assert "Agent Sherlock Gmail" in output
    assert "Gmail is connected: person@example.com" in output


def test_interactive_menu_opens_discord_menu(monkeypatch, capsys):
    choices = iter(["2", "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))
    monkeypatch.setattr(
        connections_discord,
        "discord_status",
        lambda: DiscordStatus(
            connected=True,
            bot_username="sherlock_bot",
            guild_id=8,
            channel_id=9,
            channel_name="alerts",
        ),
    )

    assert connections.run_interactive_menu() == 0

    output = capsys.readouterr().out
    assert "Agent Sherlock Discord" in output
    assert "1. Connect Discord" in output
    assert "@sherlock_bot watching #alerts (9)" in output


def test_thread_safe_terminal_writes_do_not_overlap():
    class SlowStream:
        def __init__(self):
            self.active = False
            self.overlapped = False
            self.lines = []
            self.state_lock = threading.Lock()

        def write(self, value):
            with self.state_lock:
                if self.active:
                    self.overlapped = True
                self.active = True
            time.sleep(0.002)
            self.lines.append(value)
            with self.state_lock:
                self.active = False

    stream = SlowStream()
    start = threading.Barrier(8)

    def write_line(index):
        start.wait()
        connections_shared.write_terminal(f"line {index}", file=stream)

    threads = [threading.Thread(target=write_line, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    assert not any(thread.is_alive() for thread in threads)
    assert stream.overlapped is False
    assert set(stream.lines) == {f"line {index}\n" for index in range(8)}
