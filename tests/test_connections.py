import argparse
import json
from pathlib import Path

import pytest

from agent_sherlock.cli import main
from agent_sherlock.commands import connections
from agent_sherlock.integrations.gmail import (
    GmailAuthenticationError,
    GmailFetchResult,
    GmailMessage,
    GmailProfile,
    GmailStatus,
)


def test_connections_command_appears_in_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == 0
    assert "connections" in capsys.readouterr().out


def test_connections_without_provider_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(connections, "_is_interactive_terminal", lambda: False)

    assert main(["connections"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections" in output
    assert "gmail" in output


def test_gmail_without_action_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(connections, "_is_interactive_terminal", lambda: False)

    assert main(["connections", "gmail"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections gmail" in output
    assert "connect" in output
    assert "fetch" in output
    assert "watch" in output
    assert "status" in output


def test_gmail_without_action_opens_gmail_menu_in_terminal(monkeypatch):
    monkeypatch.setattr(connections, "_is_interactive_terminal", lambda: True)
    monkeypatch.setattr(connections, "run_gmail_menu", lambda: 17)

    def unexpected_root_menu():
        raise AssertionError("The root connections menu should not open.")

    monkeypatch.setattr(connections, "run_interactive_menu", unexpected_root_menu)

    assert main(["connections", "gmail"]) == 17


def test_gmail_connect_requires_credentials_without_terminal(monkeypatch, capsys):
    monkeypatch.setattr(connections, "_is_interactive_terminal", lambda: False)

    assert main(["connections", "gmail", "connect"]) == 2

    assert "provide --credentials" in capsys.readouterr().err


def test_gmail_connect_prints_connected_account(monkeypatch, capsys):
    seen = []

    def fake_connect(path):
        seen.append(path)
        return GmailProfile(
            email_address="person@example.com",
            history_id="123",
        )

    monkeypatch.setattr(connections, "connect_gmail", fake_connect)

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
    output = capsys.readouterr().out
    assert "Gmail connected: person@example.com" in output
    assert "bodies remain inaccessible" in output


def test_gmail_fetch_missing_connection_returns_nonzero(monkeypatch, capsys):
    def fail_to_open():
        raise GmailAuthenticationError("Gmail is not connected.")

    monkeypatch.setattr(connections, "open_gmail_mailbox", fail_to_open)

    assert main(["connections", "gmail", "fetch"]) == 1
    assert "Gmail is not connected" in capsys.readouterr().err


def test_fetch_output_strips_terminal_control_characters(monkeypatch, capsys):
    message = GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender="\N{ESCAPE}[31mAttacker\N{ESCAPE}[0m",
        subject="Hello\nInjected line",
        date="Today",
        internal_date=1,
    )
    monkeypatch.setattr(connections, "open_gmail_mailbox", lambda: object())
    monkeypatch.setattr(
        connections,
        "fetch_new_gmail_messages",
        lambda _mailbox: GmailFetchResult(messages=(message,)),
    )

    assert main(["connections", "gmail", "fetch"]) == 0

    output = capsys.readouterr().out
    assert "\N{ESCAPE}" not in output
    assert "Subject: Hello Injected line" in output
    assert output.count("New Gmail message") == 1


def test_fetch_json_escapes_untrusted_control_characters(monkeypatch, capsys):
    message = GmailMessage(
        message_id="m1",
        thread_id="t1",
        sender="\N{ESCAPE}]0;malicious",
        subject="Subject",
        date="Today",
        internal_date=1,
    )
    monkeypatch.setattr(connections, "open_gmail_mailbox", lambda: object())
    monkeypatch.setattr(
        connections,
        "fetch_new_gmail_messages",
        lambda _mailbox: GmailFetchResult(messages=(message,)),
    )

    assert main(["connections", "gmail", "fetch", "--json"]) == 0

    output = capsys.readouterr().out
    assert "\N{ESCAPE}" not in output
    parsed = json.loads(output)
    assert parsed["count"] == 1
    assert parsed["messages"][0]["from"].startswith("\N{ESCAPE}")


def test_gmail_watch_rejects_nonpositive_interval(capsys):
    assert connections.run_gmail_watch(argparse.Namespace(interval=0, json=False)) == 2
    assert "--interval must be greater than 0" in capsys.readouterr().err


def test_gmail_watch_stops_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(connections, "open_gmail_mailbox", lambda: object())
    monkeypatch.setattr(
        connections,
        "fetch_new_gmail_messages",
        lambda _mailbox: GmailFetchResult(),
    )

    def stop(_delay):
        raise KeyboardInterrupt

    monkeypatch.setattr(connections.time, "sleep", stop)

    assert connections.run_gmail_watch(argparse.Namespace(interval=30, json=False)) == 0
    assert "Stopped Gmail watch" in capsys.readouterr().out


def test_gmail_watch_does_not_retry_nonretryable_api_error(monkeypatch, capsys):
    monkeypatch.setattr(connections, "open_gmail_mailbox", lambda: object())

    def forbidden(_mailbox):
        raise connections.GmailAPIError("Access denied.", status=403)

    monkeypatch.setattr(connections, "fetch_new_gmail_messages", forbidden)

    assert connections.run_gmail_watch(argparse.Namespace(interval=30, json=False)) == 1
    assert "Access denied" in capsys.readouterr().err


def test_gmail_watch_retries_rate_limit_with_backoff(monkeypatch, capsys):
    monkeypatch.setattr(connections, "open_gmail_mailbox", lambda: object())

    def rate_limited(_mailbox):
        raise connections.GmailAPIError(
            "Rate limited.",
            status=403,
            reasons=frozenset({"userRateLimitExceeded"}),
        )

    delays = []

    def stop_after_delay(delay):
        delays.append(delay)
        raise KeyboardInterrupt

    monkeypatch.setattr(connections, "fetch_new_gmail_messages", rate_limited)
    monkeypatch.setattr(connections.time, "sleep", stop_after_delay)

    assert connections.run_gmail_watch(argparse.Namespace(interval=30, json=False)) == 0
    assert delays == [30]
    assert "Retrying in 30 seconds" in capsys.readouterr().err


def test_interactive_menu_dispatches_gmail_selection(monkeypatch, capsys):
    choices = iter(["1", "4"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))
    monkeypatch.setattr(
        connections,
        "gmail_status",
        lambda: GmailStatus(connected=True, email_address="person@example.com"),
    )

    assert connections.run_interactive_menu() == 0

    output = capsys.readouterr().out
    assert "1. Gmail" in output
    assert "2. Discord" in output
    assert "Agent Sherlock Gmail" in output
    assert "Gmail is connected: person@example.com" in output


def test_interactive_menu_opens_discord_placeholder(monkeypatch, capsys):
    choices = iter(["2", "1"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))

    assert connections.run_interactive_menu() == 0

    output = capsys.readouterr().out
    assert "Agent Sherlock Discord" in output
    assert "1. Soon" in output
    assert "Discord integration coming soon." in output
