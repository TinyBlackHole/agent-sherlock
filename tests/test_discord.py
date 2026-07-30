import asyncio
import json
import os
import stat
import threading
from types import SimpleNamespace

import pytest

from agent_sherlock.integrations import discord

TOKEN = "discord-bot-token-with-enough-characters"


class FakeDiscordClient:
    def __init__(self):
        self.profile = discord.DiscordProfile(bot_id=42, username="sherlock_bot")
        self.channel = discord.DiscordChannel(
            channel_id=9,
            guild_id=8,
            name="alerts",
            channel_type=0,
        )

    def get_me(self):
        return self.profile

    def get_channel(self, _channel_id):
        return self.channel


def credentials():
    return discord.DiscordCredentials(
        token=TOKEN,
        bot_id=42,
        bot_username="sherlock_bot",
        guild_id=8,
        channel_id=9,
        channel_name="alerts",
    )


def test_connect_discord_validates_and_saves_credentials(tmp_path):
    paths = discord.DiscordPaths(tmp_path / "discord")
    client = FakeDiscordClient()

    status = discord.connect_discord(
        TOKEN,
        channel_id=9,
        paths=paths,
        client_factory=lambda _token: client,
    )

    assert status == discord.DiscordStatus(
        connected=True,
        bot_username="sherlock_bot",
        guild_id=8,
        channel_id=9,
        channel_name="alerts",
    )
    assert json.loads(paths.credentials.read_text()) == {
        "bot_id": 42,
        "bot_username": "sherlock_bot",
        "channel_id": 9,
        "channel_name": "alerts",
        "guild_id": 8,
        "schema_version": 1,
        "token": TOKEN,
    }
    if os.name == "posix":
        assert stat.S_IMODE(paths.credentials.stat().st_mode) == 0o600


def test_connect_discord_does_not_save_failed_channel_validation(tmp_path):
    paths = discord.DiscordPaths(tmp_path / "discord")
    client = FakeDiscordClient()

    def reject_channel(_channel_id):
        raise discord.DiscordConfigurationError("unsupported channel")

    client.get_channel = reject_channel

    with pytest.raises(discord.DiscordConfigurationError, match="unsupported"):
        discord.connect_discord(
            TOKEN,
            channel_id=9,
            paths=paths,
            client_factory=lambda _token: client,
        )

    assert not paths.credentials.exists()


def test_connect_discord_maps_unauthorized_token(tmp_path):
    paths = discord.DiscordPaths(tmp_path / "discord")
    client = FakeDiscordClient()

    def reject_token():
        raise discord.DiscordAPIError("unauthorized", status=401)

    client.get_me = reject_token

    with pytest.raises(
        discord.DiscordAuthenticationError,
        match="rejected",
    ):
        discord.connect_discord(
            TOKEN,
            channel_id=9,
            paths=paths,
            client_factory=lambda _token: client,
        )


def test_discord_client_rejects_channel_types_without_direct_messages(monkeypatch):
    client = discord.DiscordClient(TOKEN)
    monkeypatch.setattr(
        client,
        "_request",
        lambda _path: {
            "guild_id": "8",
            "id": "9",
            "name": "forum",
            "type": 15,
        },
    )

    with pytest.raises(discord.DiscordConfigurationError, match="text"):
        client.get_channel(9)


def test_discord_status_rejects_invalid_stored_token(tmp_path):
    paths = discord.DiscordPaths(tmp_path / "discord")
    discord.atomic_write_json(
        paths.credentials,
        {
            "bot_id": 42,
            "bot_username": "sherlock_bot",
            "channel_id": 9,
            "channel_name": "alerts",
            "guild_id": 8,
            "schema_version": 1,
            "token": "short",
        },
    )

    with pytest.raises(discord.DiscordConfigurationError, match="token"):
        discord.discord_status(paths=paths)


def test_discord_api_error_retryability():
    assert discord.DiscordAPIError("network").retryable is True
    assert discord.DiscordAPIError("rate limit", status=429).retryable is True
    assert discord.DiscordAPIError("server", status=500).retryable is True
    assert discord.DiscordAPIError("forbidden", status=403).retryable is False


@pytest.mark.parametrize("status", [401, 502])
def test_discord_client_preserves_http_status_for_non_json_errors(
    monkeypatch,
    status,
):
    class FakeResponse:
        def read(self, _limit):
            return b"<html>upstream error</html>"

        def getheader(self, _name):
            return None

    class FakeConnection:
        def __init__(self):
            self.response = FakeResponse()
            self.response.status = status

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            pass

    monkeypatch.setattr(
        discord,
        "HTTPSConnection",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    with pytest.raises(discord.DiscordAPIError) as error:
        discord.DiscordClient(TOKEN)._request("/users/@me")

    assert error.value.status == status
    assert f"HTTP {status}" in str(error.value)


def test_connect_discord_maps_non_json_unauthorized_response(monkeypatch, tmp_path):
    class FakeResponse:
        status = 401

        def read(self, _limit):
            return b"Unauthorized"

        def getheader(self, _name):
            return None

    class FakeConnection:
        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return FakeResponse()

        def close(self):
            pass

    monkeypatch.setattr(
        discord,
        "HTTPSConnection",
        lambda *_args, **_kwargs: FakeConnection(),
    )

    with pytest.raises(discord.DiscordAuthenticationError, match="rejected"):
        discord.connect_discord(
            TOKEN,
            channel_id=9,
            paths=discord.DiscordPaths(tmp_path / "discord"),
        )


def test_watch_discord_requests_minimal_intents_and_dispatches_messages():
    seen = []
    ready = []
    maintenance_calls = []
    clients = []
    fake_message = SimpleNamespace(id=100)

    class FakeIntents:
        guilds = False
        guild_messages = False
        message_content = False

        @classmethod
        def none(cls):
            return cls()

    class FakeClient:
        def __init__(self, *, intents, max_messages):
            self.intents = intents
            self.max_messages = max_messages
            self.user = SimpleNamespace(id=42)
            self.closed = False
            clients.append(self)

        def run(self, token, *, log_handler):
            assert token == TOKEN
            assert log_handler is None

            async def dispatch():
                await self.setup_hook()
                await self.on_ready()
                await self.on_message(fake_message)
                await asyncio.sleep(0.005)
                await self.close()

            asyncio.run(dispatch())

        async def close(self):
            self.closed = True

        def is_closed(self):
            return self.closed

    class LoginFailure(Exception):
        pass

    class PrivilegedIntentsRequired(Exception):
        pass

    fake_discord = SimpleNamespace(
        Client=FakeClient,
        Intents=FakeIntents,
        LoginFailure=LoginFailure,
        PrivilegedIntentsRequired=PrivilegedIntentsRequired,
    )

    discord.watch_discord(
        credentials(),
        seen.append,
        on_ready_callback=lambda: ready.append(True),
        on_maintenance_callback=lambda: maintenance_calls.append(True),
        maintenance_interval=0.001,
        discord_module=fake_discord,
    )

    assert seen == [fake_message]
    assert ready == [True]
    assert maintenance_calls
    assert len(clients) == 1
    assert clients[0].intents.guilds is True
    assert clients[0].intents.guild_messages is True
    assert clients[0].intents.message_content is True
    assert clients[0].max_messages is None


def _gateway_module(client_type, *, login_failure=None, privileged_intents=None):
    class FakeIntents:
        @classmethod
        def none(cls):
            return cls()

    return SimpleNamespace(
        Client=client_type,
        Intents=FakeIntents,
        LoginFailure=login_failure or type("LoginFailure", (Exception,), {}),
        PrivilegedIntentsRequired=privileged_intents
        or type("PrivilegedIntentsRequired", (Exception,), {}),
    )


@pytest.mark.parametrize(
    ("exception_name", "expected_error", "message"),
    [
        (
            "LoginFailure",
            discord.DiscordAuthenticationError,
            "saved bot token",
        ),
        (
            "PrivilegedIntentsRequired",
            discord.DiscordConfigurationError,
            "Message Content Intent",
        ),
    ],
)
def test_watch_discord_maps_gateway_configuration_errors(
    exception_name,
    expected_error,
    message,
):
    gateway_exception = type(exception_name, (Exception,), {})

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            raise gateway_exception

    fake_discord = _gateway_module(
        FakeClient,
        login_failure=(gateway_exception if exception_name == "LoginFailure" else None),
        privileged_intents=(
            gateway_exception if exception_name == "PrivilegedIntentsRequired" else None
        ),
    )

    with pytest.raises(expected_error, match=message):
        discord.watch_discord(
            credentials(),
            lambda _message: None,
            discord_module=fake_discord,
        )


def test_watch_discord_rejects_mismatched_bot_identity():
    class FakeClient:
        def __init__(self, **_kwargs):
            self.user = SimpleNamespace(id=999)
            self.closed = False

        def run(self, *_args, **_kwargs):
            async def dispatch():
                await self.setup_hook()
                await self.on_ready()

            asyncio.run(dispatch())

        async def close(self):
            self.closed = True

        def is_closed(self):
            return self.closed

    with pytest.raises(discord.DiscordAuthenticationError, match="does not match"):
        discord.watch_discord(
            credentials(),
            lambda _message: None,
            discord_module=_gateway_module(FakeClient),
        )


def test_watch_discord_drops_in_flight_callbacks_after_failure():
    callback_started = threading.Event()
    release_callback = threading.Event()
    seen = []

    def fail_first_callback(message):
        seen.append(message)
        callback_started.set()
        release_callback.wait(timeout=1)
        raise RuntimeError("processing failed")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.user = SimpleNamespace(id=42)
            self.closed = False

        def run(self, *_args, **_kwargs):
            async def dispatch():
                await self.setup_hook()
                await self.on_ready()
                first = asyncio.create_task(self.on_message("first"))
                await asyncio.to_thread(callback_started.wait, 1)
                second = asyncio.create_task(self.on_message("second"))
                release_callback.set()
                await asyncio.gather(first, second)

            asyncio.run(dispatch())

        async def close(self):
            self.closed = True

        def is_closed(self):
            return self.closed

    with pytest.raises(discord.DiscordWatchError, match="processing stopped"):
        discord.watch_discord(
            credentials(),
            fail_first_callback,
            discord_module=_gateway_module(FakeClient),
        )

    assert seen == ["first"]


def test_watch_discord_keyboard_interrupt_does_not_hide_prior_failure():
    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            self.failure = discord.DiscordConfigurationError("prior failure")
            raise KeyboardInterrupt

    with pytest.raises(discord.DiscordConfigurationError, match="prior failure"):
        discord.watch_discord(
            credentials(),
            lambda _message: None,
            discord_module=_gateway_module(FakeClient),
        )
