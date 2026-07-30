import argparse
import json

import pytest

from agent_sherlock.cli import main
from agent_sherlock.commands import connections_discord, output, output_telegram
from agent_sherlock.destinations import active
from agent_sherlock.destinations.discord import DiscordDestination
from agent_sherlock.integrations.discord_webhook import (
    DiscordWebhookCredentials,
    DiscordWebhookStatus,
)
from agent_sherlock.integrations.telegram import TelegramStatus

WEBHOOK_ID = "123456789012345678"
WEBHOOK_URL = f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{'a' * 60}"


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERLOCK_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


def _credentials(*, channel_id=555):
    return DiscordWebhookCredentials(
        url=WEBHOOK_URL,
        webhook_id=int(WEBHOOK_ID),
        channel_id=channel_id,
        guild_id=777,
        name="sherlock-inbox",
    )


class FakeWebhookClient:
    def __init__(self, _url=WEBHOOK_URL):
        self.sent = []

    def send_message(self, text):
        self.sent.append(text)


def test_telegram_stays_the_destination_until_it_is_changed(config_dir):
    assert active.active_destination_name() == "telegram"


def test_use_discord_switches_the_destination(config_dir, monkeypatch, capsys):
    monkeypatch.setattr(
        DiscordDestination,
        "open",
        classmethod(lambda cls: cls(_credentials(), client=FakeWebhookClient())),
    )

    assert main(["output", "use", "discord"]) == 0
    assert "delivers every message to discord" in capsys.readouterr().out
    assert active.active_destination_name() == "discord"
    assert json.loads((config_dir / "destination.json").read_text()) == {
        "destination": "discord"
    }


def test_use_discord_fails_when_the_webhook_is_not_connected(
    config_dir,
    capsys,
):
    assert main(["output", "use", "discord"]) == 1
    assert "not connected" in capsys.readouterr().err
    # A failed switch must not strand Sherlock on an unusable destination.
    assert active.active_destination_name() == "telegram"


def test_use_rejects_an_unknown_destination_without_exiting(config_dir, capsys):
    assert main(["output", "use", "signal"]) == 2
    assert "telegram or discord" in capsys.readouterr().err


def test_use_requires_a_destination_without_exiting(config_dir, capsys):
    assert main(["output", "use"]) == 2
    assert "telegram or discord" in capsys.readouterr().err


def test_a_corrupt_selection_is_reported_instead_of_guessed(config_dir):
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "destination.json").write_text('{"destination": "carrier-pigeon"}')

    with pytest.raises(active.DestinationSelectionError):
        active.active_destination_name()


def test_output_status_shows_the_active_destination_and_connections(
    config_dir,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        output,
        "telegram_status",
        lambda: TelegramStatus(connected=True, bot_username="sherlock_bot", chat_id=7),
    )
    monkeypatch.setattr(
        output,
        "discord_webhook_status",
        lambda: DiscordWebhookStatus(
            connected=True,
            name="sherlock-inbox",
            channel_id=555,
            guild_id=777,
        ),
    )

    assert main(["output", "status"]) == 0

    out = capsys.readouterr().out
    assert "Active output destination: telegram" in out
    assert "telegram: connected (@sherlock_bot)" in out
    assert "discord: connected (sherlock-inbox in channel 555)" in out


def test_telegram_without_action_prints_help(config_dir, monkeypatch, capsys):
    monkeypatch.setattr(output, "is_interactive_terminal", lambda: False)

    assert main(["output", "telegram"]) == 0

    printed = capsys.readouterr().out
    assert "usage: sherlock output telegram" in printed
    assert "connect" in printed
    assert "test" in printed
    assert "status" in printed


def test_telegram_connect_reads_token_file(
    config_dir,
    monkeypatch,
    tmp_path,
    capsys,
):
    token_file = tmp_path / "telegram-token"
    token_file.write_text("123456:abcdefghijklmnopqrstuvwxyz")
    seen = []

    def fake_connect(token, **kwargs):
        seen.append((token, kwargs["chat_id"]))
        return TelegramStatus(connected=True, bot_username="sherlock_bot", chat_id=7)

    monkeypatch.setattr(output_telegram, "connect_telegram", fake_connect)

    assert (
        main(
            [
                "output",
                "telegram",
                "connect",
                "--token-file",
                str(token_file),
                "--chat-id",
                "7",
            ]
        )
        == 0
    )

    assert seen == [("123456:abcdefghijklmnopqrstuvwxyz", 7)]
    assert "Telegram connected: @sherlock_bot" in capsys.readouterr().out


def test_telegram_connect_requires_secure_token_input(
    config_dir,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        output_telegram,
        "is_interactive_terminal",
        lambda: False,
    )

    assert main(["output", "telegram", "connect"]) == 2

    assert "--token-file" in capsys.readouterr().err


def test_telegram_status(config_dir, monkeypatch, capsys):
    monkeypatch.setattr(
        output_telegram,
        "telegram_status",
        lambda: TelegramStatus(
            connected=True,
            bot_username="sherlock_bot",
            chat_id=7,
        ),
    )

    assert main(["output", "telegram", "status"]) == 0
    assert capsys.readouterr().out == "Telegram is connected: @sherlock_bot\n"


def test_output_menu_opens_telegram_output_menu(
    config_dir,
    monkeypatch,
    capsys,
):
    choices = iter(["1", "3"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(choices))
    monkeypatch.setattr(
        output_telegram,
        "telegram_status",
        lambda: TelegramStatus(
            connected=True,
            bot_username="sherlock_bot",
            chat_id=7,
        ),
    )

    assert output.run_menu() == 0

    printed = capsys.readouterr().out
    assert "1. Manage Telegram output" in printed
    assert "Agent Sherlock Telegram output" in printed
    assert "Telegram is connected: @sherlock_bot" in printed


def test_output_discord_connect_saves_the_webhook(
    config_dir,
    tmp_path,
    monkeypatch,
    capsys,
):
    url_file = tmp_path / "webhook-url"
    url_file.write_text(f"{WEBHOOK_URL}\n")
    connected_urls = []

    def fake_connect(url, **_kwargs):
        connected_urls.append(url)
        return DiscordWebhookStatus(
            connected=True,
            name="sherlock-inbox",
            channel_id=555,
            guild_id=777,
        )

    monkeypatch.setattr(output, "connect_discord_webhook", fake_connect)

    assert (
        main(["output", "discord", "connect", "--webhook-url-file", str(url_file)]) == 0
    )

    assert connected_urls == [WEBHOOK_URL]
    out = capsys.readouterr().out
    assert "Discord output connected: sherlock-inbox in channel 555" in out
    assert "sherlock output use discord" in out


def test_output_discord_connect_reports_an_unreadable_url_file(config_dir, capsys):
    assert (
        main(
            [
                "output",
                "discord",
                "connect",
                "--webhook-url-file",
                "/nonexistent/webhook-url",
            ]
        )
        == 2
    )
    assert "cannot read Discord webhook URL file" in capsys.readouterr().err


def test_output_test_sends_through_the_active_destination(
    config_dir,
    monkeypatch,
    capsys,
):
    client = FakeWebhookClient()
    monkeypatch.setattr(
        active,
        "active_destination_name",
        lambda **_kwargs: "discord",
    )
    monkeypatch.setattr(
        DiscordDestination,
        "open",
        classmethod(lambda cls: cls(_credentials(), client=client)),
    )

    assert main(["output", "test"]) == 0
    assert client.sent == [output.TEST_MESSAGE]
    assert "Test message sent to discord." in capsys.readouterr().out


def test_active_destination_observes_switches_while_running(
    config_dir,
    monkeypatch,
):
    class RecordingDestination:
        def __init__(self, name):
            self.name = name
            self.messages = []

        def send(self, text):
            self.messages.append(text)

    telegram = RecordingDestination("telegram")
    discord = RecordingDestination("discord")
    destinations = {"telegram": telegram, "discord": discord}
    monkeypatch.setattr(active, "open_destination", destinations.__getitem__)

    destination = active.ActiveDestination.open()
    destination.send("before switch")
    active.set_active_destination("discord")
    destination.send("after switch")

    assert telegram.messages == ["before switch"]
    assert discord.messages == ["after switch"]


def test_active_destination_open_runs_the_startup_validator(
    config_dir,
    monkeypatch,
):
    class TelegramLike:
        name = "telegram"

        def send(self, _text):
            pass

    destination = TelegramLike()
    monkeypatch.setattr(active, "open_destination", lambda _name: destination)

    def reject(candidate):
        assert candidate is destination
        raise connections_discord.DiscordConfigurationError("invalid pairing")

    with pytest.raises(
        connections_discord.DiscordConfigurationError,
        match="invalid pairing",
    ):
        active.ActiveDestination.open(validator=reject)


def test_watching_the_discord_output_channel_is_refused(config_dir):
    destination = DiscordDestination(
        _credentials(channel_id=555),
        client=FakeWebhookClient(),
    )

    with pytest.raises(connections_discord.DiscordConfigurationError) as error:
        connections_discord.reject_delivery_loop(555, destination)

    assert "back to itself" in str(error.value)


def test_watching_a_different_discord_channel_is_allowed(config_dir):
    destination = DiscordDestination(
        _credentials(channel_id=555),
        client=FakeWebhookClient(),
    )

    connections_discord.reject_delivery_loop(999, destination)


def test_the_loop_guard_ignores_a_non_discord_destination(config_dir):
    class TelegramLike:
        name = "telegram"

    connections_discord.reject_delivery_loop(555, TelegramLike())


def test_dynamic_destination_rechecks_the_discord_loop_guard(
    config_dir,
    monkeypatch,
):
    class TelegramLike:
        name = "telegram"

        def send(self, _text):
            pass

    client = FakeWebhookClient()
    discord = DiscordDestination(
        _credentials(channel_id=555),
        client=client,
    )
    destinations = {"telegram": TelegramLike(), "discord": discord}
    monkeypatch.setattr(active, "open_destination", destinations.__getitem__)

    destination = active.ActiveDestination.open(
        validator=lambda candidate: connections_discord.reject_delivery_loop(
            555,
            candidate,
        )
    )
    active.set_active_destination("discord")

    with pytest.raises(connections_discord.DiscordConfigurationError):
        destination.send("would loop")
    assert client.sent == []


def test_output_without_an_action_prints_help_when_not_interactive(
    config_dir,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(output, "is_interactive_terminal", lambda: False)

    assert main(["output"]) == 0
    printed = capsys.readouterr().out
    assert "usage:" in printed
    assert "telegram" in printed


def test_discord_connect_without_a_url_or_a_terminal_exits_with_a_usage_code(
    config_dir,
    monkeypatch,
):
    monkeypatch.setattr(output, "is_interactive_terminal", lambda: False)

    assert output.run_discord_connect(argparse.Namespace(webhook_url_file=None)) == 2
