from datetime import UTC, datetime

import pytest

from agent_sherlock import input_settings
from agent_sherlock.application import PendingDeliveryError, PipelineError, SyncResult
from agent_sherlock.cli import main
from agent_sherlock.commands import connections, connections_discord
from agent_sherlock.domain import InboundMessage
from agent_sherlock.integrations.discord import (
    DiscordCredentials,
    DiscordStatus,
    DiscordWatchError,
)
from agent_sherlock.integrations.telegram import TelegramError
from agent_sherlock.persistence import PersistenceError

TOKEN = "discord-bot-token-with-enough-characters"


def credentials():
    return DiscordCredentials(
        token=TOKEN,
        bot_id=42,
        bot_username="sherlock_bot",
        guild_id=8,
        channel_id=9,
        channel_name="alerts",
    )


def inbound_message():
    return InboundMessage(
        source="discord",
        account_id="8",
        external_id="100",
        conversation_id="9",
        sender="Ada",
        subject="#alerts",
        body="Service is down",
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
    )


def test_discord_without_action_prints_help(monkeypatch, capsys):
    monkeypatch.setattr(connections, "is_interactive_terminal", lambda: False)

    assert main(["connections", "discord"]) == 0

    output = capsys.readouterr().out
    assert "usage: sherlock connections discord" in output
    assert "connect" in output
    assert "watch" in output
    assert "status" in output
    assert "enable" in output
    assert "disable" in output


def test_discord_connect_reads_token_file(monkeypatch, tmp_path, capsys):
    token_file = tmp_path / "discord-token"
    token_file.write_text(TOKEN)
    seen = []
    input_settings.set_input_enabled("discord", False)

    def fake_connect(token, *, channel_id):
        seen.append((token, channel_id))
        return DiscordStatus(
            connected=True,
            bot_username="sherlock_bot",
            guild_id=8,
            channel_id=9,
            channel_name="alerts",
        )

    monkeypatch.setattr(connections_discord, "connect_discord", fake_connect)

    assert (
        main(
            [
                "connections",
                "discord",
                "connect",
                "--token-file",
                str(token_file),
                "--channel-id",
                "9",
            ]
        )
        == 0
    )

    assert seen == [(TOKEN, 9)]
    assert input_settings.input_is_enabled("discord") is True
    assert "@sherlock_bot watching #alerts (9)" in capsys.readouterr().out


def test_discord_connect_reports_success_before_auto_enable_warning(
    monkeypatch,
    tmp_path,
    capsys,
):
    token_file = tmp_path / "discord-token"
    token_file.write_text(TOKEN)
    monkeypatch.setattr(
        connections_discord,
        "connect_discord",
        lambda _token, *, channel_id: DiscordStatus(
            connected=True,
            bot_username="sherlock_bot",
            guild_id=8,
            channel_id=channel_id,
            channel_name="alerts",
        ),
    )
    monkeypatch.setattr(
        connections_discord,
        "set_input_enabled",
        lambda *_args: (_ for _ in ()).throw(
            input_settings.InputSettingsError("settings unavailable")
        ),
    )

    assert (
        main(
            [
                "connections",
                "discord",
                "connect",
                "--token-file",
                str(token_file),
                "--channel-id",
                "9",
            ]
        )
        == 1
    )

    output = capsys.readouterr()
    assert "Discord connected: @sherlock_bot watching #alerts (9)" in output.out
    assert "Warning: Discord is connected" in output.err


def test_discord_connect_requires_secure_inputs(monkeypatch, capsys):
    monkeypatch.setattr(
        connections_discord,
        "is_interactive_terminal",
        lambda: False,
    )

    assert main(["connections", "discord", "connect"]) == 2
    assert "--token-file" in capsys.readouterr().err


def test_discord_status(monkeypatch, capsys):
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
    monkeypatch.setattr(
        connections_discord,
        "MessageRepository",
        lambda: type("Repository", (), {"dead_letter_count": lambda self: 2})(),
    )

    assert main(["connections", "discord", "status"]) == 0
    assert capsys.readouterr().out == (
        "Discord is connected: @sherlock_bot watching #alerts (9).\n"
        "Automatic watch: enabled.\n"
        "Dead-letter queue: 2 messages.\n"
    )


def test_discord_can_be_paused_and_enabled_without_reconnecting(
    monkeypatch,
    capsys,
):
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

    assert main(["connections", "discord", "disable"]) == 0
    assert input_settings.input_is_enabled("discord") is False
    assert "remains connected" in capsys.readouterr().out

    assert main(["connections", "discord", "enable"]) == 0
    assert input_settings.input_is_enabled("discord") is True
    assert "automatic watching enabled" in capsys.readouterr().out


def test_discord_cannot_be_enabled_before_it_is_connected(monkeypatch, capsys):
    monkeypatch.setattr(
        connections_discord,
        "discord_status",
        lambda: DiscordStatus(connected=False),
    )

    assert main(["connections", "discord", "enable"]) == 1
    assert "connect Discord" in capsys.readouterr().err


def test_discord_watch_ingests_gateway_message(monkeypatch, capsys):
    normalized = inbound_message()

    class Connector:
        credentials = credentials()

        def normalize(self, message):
            assert message == "gateway-event"
            return normalized

    class Pipeline:
        def ingest(self, messages):
            assert messages == (normalized,)
            return SyncResult(discovered=1, stored=1, delivered=1)

        def deliver_pending(self):
            return SyncResult()

    def fake_watch(
        discord_credentials,
        handler,
        *,
        on_ready_callback,
        on_maintenance_callback,
        stop_event,
    ):
        assert discord_credentials == Connector.credentials
        assert stop_event is None
        on_ready_callback()
        handler("gateway-event")
        on_maintenance_callback()

    monkeypatch.setattr(
        connections_discord,
        "_open_pipeline",
        lambda: (Connector(), Pipeline()),
    )
    monkeypatch.setattr(connections_discord, "watch_discord", fake_watch)

    assert main(["connections", "discord", "watch"]) == 0

    output = capsys.readouterr().out
    assert "Forwarding Discord #alerts to telegram" in output
    assert "Sent 1 queued message to the configured output" in output
    assert "Stopped Discord watch" in output


def test_discord_watch_keeps_gateway_alive_after_pipeline_errors(monkeypatch, capsys):
    normalized = inbound_message()

    class Connector:
        credentials = credentials()

        def normalize(self, _message):
            return normalized

    class Pipeline:
        def ingest(self, _messages):
            raise PersistenceError("database is locked")

        def deliver_pending(self):
            raise PipelineError("cannot process queued message")

    def fake_watch(
        _credentials,
        handler,
        *,
        on_ready_callback,
        on_maintenance_callback,
        stop_event,
    ):
        assert stop_event is None
        on_ready_callback()
        handler("gateway-event")
        on_maintenance_callback()

    monkeypatch.setattr(
        connections_discord,
        "_open_pipeline",
        lambda: (Connector(), Pipeline()),
    )
    monkeypatch.setattr(connections_discord, "watch_discord", fake_watch)

    assert main(["connections", "discord", "watch"]) == 0

    output = capsys.readouterr()
    assert output.err.count("Discord watch remains connected") == 2
    assert "Stopped Discord watch" in output.out


def test_discord_watch_explains_how_to_resolve_an_output_conflict(
    monkeypatch,
    capsys,
):
    normalized = inbound_message()
    conflict = connections_discord.DiscordConfigurationError(
        "The watched channel is also the output channel."
    )

    class Connector:
        credentials = credentials()

        def normalize(self, _message):
            return normalized

    class Pipeline:
        def ingest(self, _messages):
            raise PendingDeliveryError(conflict)

        def deliver_pending(self):
            raise PendingDeliveryError(conflict)

    def fake_watch(
        _credentials,
        handler,
        *,
        on_ready_callback,
        on_maintenance_callback,
        stop_event,
    ):
        assert stop_event is None
        on_ready_callback()
        handler("gateway-event")
        on_maintenance_callback()

    monkeypatch.setattr(
        connections_discord,
        "_open_pipeline",
        lambda: (Connector(), Pipeline()),
    )
    monkeypatch.setattr(connections_discord, "watch_discord", fake_watch)

    assert main(["connections", "discord", "watch"]) == 0

    output = capsys.readouterr()
    assert output.err.count("Change the output configuration") == 2
    assert output.err.count("message remains queued until then") == 2
    assert "Queued delivery will be retried" not in output.err


@pytest.mark.parametrize(
    "exception",
    [
        TelegramError("Telegram unavailable"),
        PersistenceError("database unavailable"),
    ],
)
def test_discord_watch_open_failures_return_one(monkeypatch, capsys, exception):
    def fail_open():
        raise exception

    monkeypatch.setattr(connections_discord, "_open_pipeline", fail_open)

    assert main(["connections", "discord", "watch"]) == 1
    assert str(exception) in capsys.readouterr().err


def test_discord_watch_gateway_failure_returns_one(monkeypatch, capsys):
    class Connector:
        credentials = credentials()

    monkeypatch.setattr(
        connections_discord,
        "_open_pipeline",
        lambda: (Connector(), object()),
    )
    monkeypatch.setattr(
        connections_discord,
        "watch_discord",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            DiscordWatchError("Gateway unavailable")
        ),
    )

    assert main(["connections", "discord", "watch"]) == 1
    assert "Gateway unavailable" in capsys.readouterr().err


def test_discord_custom_ready_message_is_announced_by_on_ready(
    monkeypatch,
    capsys,
):
    callback_seen = []

    class Connector:
        credentials = credentials()

    class Pipeline:
        pass

    def fake_watch(
        _credentials,
        _handler,
        *,
        on_ready_callback,
        on_maintenance_callback,
        stop_event,
    ):
        assert on_maintenance_callback is not None
        assert stop_event is not None
        assert "✓ Discord" not in capsys.readouterr().out
        on_ready_callback()
        callback_seen.append(True)

    monkeypatch.setattr(connections_discord, "watch_discord", fake_watch)

    assert (
        connections_discord.watch_connected_discord(
            Connector(),
            Pipeline(),
            destination_name="telegram",
            stop_event=type("StopEvent", (), {})(),
            ready_message="✓ Discord: watching #alerts",
            announce_stop=False,
        )
        == 0
    )

    assert callback_seen == [True]
    assert capsys.readouterr().out == "✓ Discord: watching #alerts\n"


def test_interactive_channel_id_rejects_invalid_input(monkeypatch):
    monkeypatch.setattr(
        connections_discord,
        "is_interactive_terminal",
        lambda: True,
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "not-a-channel")

    assert (
        connections_discord._channel_id_from_args(
            type("Args", (), {"channel_id": None})()
        )
        is None
    )
