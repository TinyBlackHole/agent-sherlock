from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any

from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

DISCORD_API_HOST = "discord.com"
DISCORD_API_VERSION = 10
DISCORD_CREDENTIALS_SCHEMA_VERSION = 1
MAX_DISCORD_TOKEN_CHARACTERS = 512
MAX_API_RESPONSE_BYTES = 1_000_000
DEFAULT_MAINTENANCE_INTERVAL_SECONDS = 30.0

# Text channels, announcement channels, and their thread variants. Forum and
# media channels contain messages in child threads rather than in the channel
# itself, so accepting their IDs here would silently miss events.
SUPPORTED_CHANNEL_TYPES = frozenset({0, 5, 10, 11, 12})


class DiscordError(RuntimeError):
    """Base class for expected Discord integration failures."""


class DiscordDependencyError(DiscordError):
    """Raised when the Discord Gateway dependency is unavailable."""


class DiscordConfigurationError(DiscordError):
    """Raised when local Discord configuration is missing or invalid."""


class DiscordAuthenticationError(DiscordError):
    """Raised when Discord rejects a bot token."""


class DiscordAPIError(DiscordError):
    """Raised when Discord returns an API or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


class DiscordWatchError(DiscordError):
    """Raised when a Discord Gateway watcher stops unexpectedly."""


@dataclass(frozen=True, slots=True)
class DiscordPaths:
    directory: Path

    @classmethod
    def default(cls) -> DiscordPaths:
        return cls(config_root() / "connections" / "discord")

    @property
    def credentials(self) -> Path:
        return self.directory / "token.json"


@dataclass(frozen=True, slots=True)
class DiscordProfile:
    bot_id: int
    username: str


@dataclass(frozen=True, slots=True)
class DiscordChannel:
    channel_id: int
    guild_id: int
    name: str
    channel_type: int


@dataclass(frozen=True, slots=True)
class DiscordCredentials:
    token: str = field(repr=False)
    bot_id: int
    bot_username: str
    guild_id: int
    channel_id: int
    channel_name: str

    def as_json(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "bot_username": self.bot_username,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "guild_id": self.guild_id,
            "schema_version": DISCORD_CREDENTIALS_SCHEMA_VERSION,
            "token": self.token,
        }


@dataclass(frozen=True, slots=True)
class DiscordStatus:
    connected: bool
    bot_username: str = ""
    guild_id: int | None = None
    channel_id: int | None = None
    channel_name: str = ""


class DiscordClient:
    """Small synchronous client for Discord connection validation."""

    def __init__(self, token: str):
        _validate_token(token)
        self._token = token

    def get_me(self) -> DiscordProfile:
        data = self._request("/users/@me")
        bot_id = _snowflake(data.get("id"))
        username = data.get("username")
        if (
            bot_id is None
            or not isinstance(username, str)
            or not username
            or data.get("bot") is not True
        ):
            raise DiscordAPIError("Discord returned an invalid bot profile.")
        return DiscordProfile(bot_id=bot_id, username=username)

    def get_channel(self, channel_id: int) -> DiscordChannel:
        data = self._request(f"/channels/{channel_id}")
        response_channel_id = _snowflake(data.get("id"))
        guild_id = _snowflake(data.get("guild_id"))
        name = data.get("name")
        channel_type = data.get("type")
        if (
            response_channel_id != channel_id
            or guild_id is None
            or not isinstance(name, str)
            or not name
            or type(channel_type) is not int
        ):
            raise DiscordAPIError("Discord returned an invalid server channel.")
        if channel_type not in SUPPORTED_CHANNEL_TYPES:
            raise DiscordConfigurationError(
                "Discord input must be a server text, announcement, or thread channel."
            )
        return DiscordChannel(
            channel_id=response_channel_id,
            guild_id=guild_id,
            name=name,
            channel_type=channel_type,
        )

    def _request(self, path: str) -> dict[str, Any]:
        connection = HTTPSConnection(DISCORD_API_HOST, timeout=30)
        try:
            connection.request(
                "GET",
                f"/api/v{DISCORD_API_VERSION}{path}",
                headers={
                    "Authorization": f"Bot {self._token}",
                    "User-Agent": "Agent-Sherlock",
                },
            )
            response = connection.getresponse()
            raw_response = response.read(MAX_API_RESPONSE_BYTES + 1)
            status = response.status
            retry_after_header = response.getheader("Retry-After")
        except (OSError, HTTPException) as exc:
            raise DiscordAPIError("Cannot reach the Discord API.") from exc
        finally:
            connection.close()

        data: Any = None
        response_is_too_large = len(raw_response) > MAX_API_RESPONSE_BYTES
        try:
            if not response_is_too_large:
                data = json.loads(raw_response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            if 200 <= status < 300:
                raise DiscordAPIError(
                    "Discord returned an unreadable API response.",
                    status=status,
                ) from exc
        if not 200 <= status < 300:
            raise _discord_api_error(
                data if isinstance(data, dict) else {},
                status=status,
                retry_after_header=retry_after_header,
            )
        if response_is_too_large:
            raise DiscordAPIError(
                "Discord returned an unexpectedly large API response.",
                status=status,
            )
        if not isinstance(data, dict):
            raise DiscordAPIError(
                "Discord returned an invalid API response.",
                status=status,
            )
        return data


def connect_discord(
    token: str,
    *,
    channel_id: int,
    paths: DiscordPaths | None = None,
    client_factory: Callable[[str], DiscordClient] = DiscordClient,
) -> DiscordStatus:
    selected_paths = paths or DiscordPaths.default()
    if type(channel_id) is not int or channel_id <= 0:
        raise DiscordConfigurationError(
            "The Discord channel ID must be a positive integer."
        )

    client = client_factory(token)
    try:
        profile = client.get_me()
    except DiscordAPIError as exc:
        if exc.status == 401:
            raise DiscordAuthenticationError("Discord rejected the bot token.") from exc
        raise
    channel = client.get_channel(channel_id)
    credentials = DiscordCredentials(
        token=token,
        bot_id=profile.bot_id,
        bot_username=profile.username,
        guild_id=channel.guild_id,
        channel_id=channel.channel_id,
        channel_name=channel.name,
    )
    _save_credentials(selected_paths, credentials)
    return DiscordStatus(
        connected=True,
        bot_username=profile.username,
        guild_id=channel.guild_id,
        channel_id=channel.channel_id,
        channel_name=channel.name,
    )


def load_discord_credentials(
    *,
    paths: DiscordPaths | None = None,
) -> DiscordCredentials:
    selected_paths = paths or DiscordPaths.default()
    if not selected_paths.credentials.exists():
        raise DiscordAuthenticationError(
            "Discord is not connected. Run `sherlock connections discord connect`."
        )
    try:
        data = read_json_object(selected_paths.credentials, private=True)
    except StorageError as exc:
        raise DiscordConfigurationError(str(exc)) from exc

    if data.get("schema_version") != DISCORD_CREDENTIALS_SCHEMA_VERSION:
        raise DiscordConfigurationError(
            "The stored Discord connection has an unsupported format. "
            "Reconnect Discord."
        )

    token = data.get("token")
    bot_id = data.get("bot_id")
    bot_username = data.get("bot_username")
    guild_id = data.get("guild_id")
    channel_id = data.get("channel_id")
    channel_name = data.get("channel_name")
    if not isinstance(token, str):
        raise DiscordConfigurationError(
            "The stored Discord bot token is invalid. Reconnect Discord."
        )
    try:
        _validate_token(token)
    except DiscordAuthenticationError as exc:
        raise DiscordConfigurationError(
            "The stored Discord bot token is invalid. Reconnect Discord."
        ) from exc
    if (
        type(bot_id) is not int
        or bot_id <= 0
        or not isinstance(bot_username, str)
        or not bot_username
        or type(guild_id) is not int
        or guild_id <= 0
        or type(channel_id) is not int
        or channel_id <= 0
        or not isinstance(channel_name, str)
        or not channel_name
    ):
        raise DiscordConfigurationError(
            "The stored Discord connection is invalid. Reconnect Discord."
        )
    return DiscordCredentials(
        token=token,
        bot_id=bot_id,
        bot_username=bot_username,
        guild_id=guild_id,
        channel_id=channel_id,
        channel_name=channel_name,
    )


def discord_status(*, paths: DiscordPaths | None = None) -> DiscordStatus:
    selected_paths = paths or DiscordPaths.default()
    if not selected_paths.credentials.exists():
        return DiscordStatus(connected=False)
    credentials = load_discord_credentials(paths=selected_paths)
    return DiscordStatus(
        connected=True,
        bot_username=credentials.bot_username,
        guild_id=credentials.guild_id,
        channel_id=credentials.channel_id,
        channel_name=credentials.channel_name,
    )


def watch_discord(
    credentials: DiscordCredentials,
    on_message_callback: Callable[[Any], None],
    *,
    on_ready_callback: Callable[[], None] | None = None,
    on_maintenance_callback: Callable[[], None] | None = None,
    maintenance_interval: float = DEFAULT_MAINTENANCE_INTERVAL_SECONDS,
    stop_event: threading.Event | None = None,
    discord_module: Any | None = None,
) -> None:
    """Run a Discord Gateway client and hand new events to a sync callback."""
    if not math.isfinite(maintenance_interval) or maintenance_interval <= 0:
        raise DiscordConfigurationError(
            "The Discord maintenance interval must be greater than zero."
        )
    discord = discord_module or _load_discord_module()
    intents = discord.Intents.none()
    intents.guilds = True
    intents.guild_messages = True
    intents.message_content = True

    class SherlockDiscordClient(discord.Client):
        def __init__(self) -> None:
            super().__init__(intents=intents, max_messages=None)
            self.failure: Exception | None = None
            self._ingest_lock: asyncio.Lock | None = None
            self._announced_ready = False
            self._ready_event: asyncio.Event | None = None
            self._maintenance_task: asyncio.Task[None] | None = None
            self._shutdown_task: asyncio.Task[None] | None = None

        async def setup_hook(self) -> None:
            self._ingest_lock = asyncio.Lock()
            self._ready_event = asyncio.Event()
            if on_maintenance_callback is not None:
                self._maintenance_task = asyncio.create_task(
                    self._maintain_delivery_queue(),
                    name="sherlock-discord-maintenance",
                )
            if stop_event is not None:
                self._shutdown_task = asyncio.create_task(
                    self._watch_for_shutdown(),
                    name="sherlock-discord-shutdown",
                )

        async def on_ready(self) -> None:
            current_user = self.user
            current_user_id = getattr(current_user, "id", None)
            if current_user_id != credentials.bot_id:
                self.failure = DiscordAuthenticationError(
                    "The connected Discord bot does not match saved credentials. "
                    "Reconnect Discord."
                )
                await self.close()
                return
            if not self._announced_ready and on_ready_callback is not None:
                try:
                    on_ready_callback()
                except Exception as exc:
                    self.failure = exc
                    await self.close()
                    return
            self._announced_ready = True
            if self._ready_event is not None:
                self._ready_event.set()

        async def on_message(self, message: Any) -> None:
            if self._ingest_lock is None:
                self._ingest_lock = asyncio.Lock()
            try:
                async with self._ingest_lock:
                    if self.failure is not None:
                        return
                    await asyncio.to_thread(on_message_callback, message)
            except Exception as exc:
                self.failure = exc
                await self.close()

        async def _maintain_delivery_queue(self) -> None:
            if self._ready_event is None or on_maintenance_callback is None:
                return
            await self._ready_event.wait()
            while not self.is_closed():
                try:
                    if self._ingest_lock is None:
                        self._ingest_lock = asyncio.Lock()
                    async with self._ingest_lock:
                        if self.failure is not None:
                            return
                        await asyncio.to_thread(on_maintenance_callback)
                except Exception as exc:
                    self.failure = exc
                    await self.close()
                    return
                await asyncio.sleep(maintenance_interval)

        async def _watch_for_shutdown(self) -> None:
            if stop_event is None:
                return
            while not self.is_closed() and not stop_event.is_set():
                await asyncio.sleep(0.2)
            if stop_event.is_set() and not self.is_closed():
                await self.close()

    client = SherlockDiscordClient()
    try:
        client.run(credentials.token, log_handler=None)
    except KeyboardInterrupt:
        if client.failure is None:
            return
    except Exception as exc:
        login_failure = getattr(discord, "LoginFailure", ())
        privileged_intents = getattr(discord, "PrivilegedIntentsRequired", ())
        if isinstance(exc, login_failure):
            raise DiscordAuthenticationError(
                "Discord rejected the saved bot token. Reconnect Discord."
            ) from exc
        if isinstance(exc, privileged_intents):
            raise DiscordConfigurationError(
                "Enable Message Content Intent for the bot in the Discord "
                "Developer Portal, then run watch again."
            ) from exc
        raise DiscordWatchError("The Discord Gateway connection failed.") from exc

    if client.failure is not None:
        if isinstance(client.failure, DiscordError):
            raise client.failure
        raise DiscordWatchError(
            f"Discord message processing stopped: {client.failure}"
        ) from client.failure


def _load_discord_module() -> Any:
    try:
        import discord
    except ImportError as exc:
        raise DiscordDependencyError(
            "Discord Gateway support is not installed. Reinstall Agent Sherlock."
        ) from exc
    return discord


def _save_credentials(
    paths: DiscordPaths,
    credentials: DiscordCredentials,
) -> None:
    try:
        atomic_write_json(paths.credentials, credentials.as_json())
    except StorageError as exc:
        raise DiscordConfigurationError(
            "Could not securely save the Discord connection."
        ) from exc


def _validate_token(token: str) -> None:
    if (
        not token
        or len(token) > MAX_DISCORD_TOKEN_CHARACTERS
        or len(token) < 20
        or any(
            character.isspace() or not character.isprintable() for character in token
        )
    ):
        raise DiscordAuthenticationError("The Discord bot token has an invalid format.")


def _snowflake(value: Any) -> int | None:
    if not isinstance(value, str) or not value.isdecimal():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _discord_api_error(
    data: dict[str, Any],
    *,
    status: int,
    retry_after_header: str | None,
) -> DiscordAPIError:
    raw_message = data.get("message")
    safe_message = (
        " ".join(raw_message.split())[:300] if isinstance(raw_message, str) else ""
    )
    retry_after: float | None = None
    raw_retry_after = data.get("retry_after", retry_after_header)
    try:
        if raw_retry_after is not None:
            retry_after = max(0.0, float(raw_retry_after))
    except (TypeError, ValueError):
        retry_after = None
    suffix = f": {safe_message}" if safe_message else ""
    return DiscordAPIError(
        f"Discord API request failed (HTTP {status}){suffix}.",
        status=status,
        retry_after=retry_after,
    )
