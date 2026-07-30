from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

DISCORD_WEBHOOK_SCHEMA_VERSION = 1
# Only official Discord hosts are accepted so a pasted URL can never redirect
# Sherlock's output to an attacker-controlled endpoint.
DISCORD_WEBHOOK_HOSTS = frozenset(
    {
        "discord.com",
        "discordapp.com",
        "canary.discord.com",
        "ptb.discord.com",
    }
)
DISCORD_WEBHOOK_PATH_PATTERN = re.compile(
    r"^/api(?:/v\d{1,2})?/webhooks/"
    r"(?P<webhook_id>[0-9]{5,20})/(?P<token>[A-Za-z0-9_-]{40,})$"
)
DISCORD_MESSAGE_LIMIT = 2_000
DISCORD_SAFE_CONTENT_SIZE = 1_900
DISCORD_LONG_MESSAGE_PREVIEW_SIZE = 800
DISCORD_ATTACHMENT_FILENAME = "sherlock-message.txt"
# Webhook posts must never notify anyone: forwarded bodies are untrusted text
# that could otherwise contain @everyone or role mentions.
SUPPRESSED_MENTIONS: dict[str, Any] = {"parse": []}
SUPPRESS_EMBEDS_FLAG = 1 << 2


class DiscordWebhookError(RuntimeError):
    """Base class for expected Discord webhook output failures."""


class DiscordWebhookConfigurationError(DiscordWebhookError):
    """Raised when local Discord webhook configuration is missing or invalid."""


class DiscordWebhookAuthenticationError(DiscordWebhookError):
    """Raised when Discord rejects or no longer recognizes the webhook."""


class DiscordWebhookAPIError(DiscordWebhookError):
    """Raised when Discord returns an API or transport failure."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.status is None or self.status == 429 or self.status >= 500


@dataclass(frozen=True, slots=True)
class DiscordWebhookPaths:
    directory: Path

    @classmethod
    def default(cls) -> DiscordWebhookPaths:
        return cls(config_root() / "connections" / "discord-webhook")

    @property
    def credentials(self) -> Path:
        return self.directory / "webhook.json"


@dataclass(frozen=True, slots=True)
class DiscordWebhookTarget:
    webhook_id: int
    channel_id: int
    guild_id: int | None
    name: str


@dataclass(frozen=True, slots=True)
class DiscordWebhookCredentials:
    url: str = field(repr=False)
    webhook_id: int
    channel_id: int
    guild_id: int | None
    name: str

    def as_json(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "name": self.name,
            "schema_version": DISCORD_WEBHOOK_SCHEMA_VERSION,
            "url": self.url,
            "webhook_id": self.webhook_id,
        }


@dataclass(frozen=True, slots=True)
class DiscordWebhookStatus:
    connected: bool
    name: str = ""
    channel_id: int | None = None
    guild_id: int | None = None


class DiscordWebhookClient:
    """Small synchronous client for a single Discord webhook."""

    def __init__(self, url: str):
        self._host, self._path = _parse_webhook_url(url)

    def get_webhook(self) -> DiscordWebhookTarget:
        data = self._request("GET")
        if not isinstance(data, dict):
            raise DiscordWebhookAPIError("Discord returned an invalid webhook.")
        webhook_id = _snowflake(data.get("id"))
        channel_id = _snowflake(data.get("channel_id"))
        if webhook_id is None or channel_id is None:
            raise DiscordWebhookAPIError(
                "Discord returned an incomplete webhook profile."
            )
        raw_name = data.get("name")
        return DiscordWebhookTarget(
            webhook_id=webhook_id,
            channel_id=channel_id,
            guild_id=_snowflake(data.get("guild_id")),
            name=raw_name if isinstance(raw_name, str) and raw_name else "webhook",
        )

    def send_message(self, text: str) -> None:
        safe_text = text or "(empty message)"
        if len(safe_text) <= DISCORD_SAFE_CONTENT_SIZE:
            response = self._request(
                "POST",
                {
                    "allowed_mentions": SUPPRESSED_MENTIONS,
                    "content": safe_text,
                    "flags": SUPPRESS_EMBEDS_FLAG,
                },
            )
        else:
            body, content_type = _long_message_request(safe_text)
            response = self._request(
                "POST",
                raw_body=body,
                content_type=content_type,
            )
        if not isinstance(response, dict) or _snowflake(response.get("id")) is None:
            raise DiscordWebhookAPIError(
                "Discord did not confirm that the webhook message was saved."
            )

    def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        raw_body: bytes | None = None,
        content_type: str | None = None,
        timeout: int = 30,
    ) -> Any:
        if raw_body is not None:
            body = raw_body
        else:
            body = (
                json.dumps(payload, separators=(",", ":")).encode()
                if payload is not None
                else None
            )
        headers = {"User-Agent": "Agent-Sherlock"}
        if body is not None:
            headers["Content-Type"] = content_type or "application/json"
        connection = HTTPSConnection(self._host, timeout=timeout)
        try:
            path = f"{self._path}?wait=true" if method == "POST" else self._path
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw_response = response.read()
            status = response.status
        except (OSError, HTTPException) as exc:
            raise DiscordWebhookAPIError("Cannot reach the Discord API.") from exc
        finally:
            connection.close()

        if status in {401, 403, 404}:
            raise DiscordWebhookAuthenticationError(
                "Discord rejected the webhook. It may have been deleted or "
                "regenerated. Reconnect the Discord output."
            )
        if not 200 <= status < 300:
            raise _discord_webhook_api_error(raw_response, status=status)
        if not raw_response:
            return None
        try:
            return json.loads(raw_response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DiscordWebhookAPIError(
                "Discord returned an unreadable API response.",
                status=status,
            ) from exc


def connect_discord_webhook(
    url: str,
    *,
    paths: DiscordWebhookPaths | None = None,
    client_factory: Callable[[str], DiscordWebhookClient] = DiscordWebhookClient,
) -> DiscordWebhookStatus:
    """Validate a webhook URL against Discord and save it as the output."""
    selected_paths = paths or DiscordWebhookPaths.default()
    normalized_url = url.strip()
    _parse_webhook_url(normalized_url)
    target = client_factory(normalized_url).get_webhook()

    credentials = DiscordWebhookCredentials(
        url=normalized_url,
        webhook_id=target.webhook_id,
        channel_id=target.channel_id,
        guild_id=target.guild_id,
        name=target.name,
    )
    _save_credentials(selected_paths, credentials)
    return DiscordWebhookStatus(
        connected=True,
        name=target.name,
        channel_id=target.channel_id,
        guild_id=target.guild_id,
    )


def load_discord_webhook_credentials(
    *,
    paths: DiscordWebhookPaths | None = None,
) -> DiscordWebhookCredentials:
    selected_paths = paths or DiscordWebhookPaths.default()
    if not selected_paths.credentials.exists():
        raise DiscordWebhookAuthenticationError(
            "The Discord output is not connected. Run "
            "`sherlock output discord connect`."
        )
    try:
        data = read_json_object(selected_paths.credentials, private=True)
    except StorageError as exc:
        raise DiscordWebhookConfigurationError(str(exc)) from exc

    url = data.get("url")
    webhook_id = data.get("webhook_id")
    channel_id = data.get("channel_id")
    guild_id = data.get("guild_id")
    name = data.get("name")
    if not isinstance(url, str):
        raise DiscordWebhookConfigurationError(
            "The stored Discord webhook URL is invalid. Reconnect the Discord output."
        )
    try:
        _parse_webhook_url(url)
    except DiscordWebhookConfigurationError as exc:
        raise DiscordWebhookConfigurationError(
            "The stored Discord webhook URL is invalid. Reconnect the Discord output."
        ) from exc
    if (
        type(webhook_id) is not int
        or webhook_id <= 0
        or type(channel_id) is not int
        or channel_id <= 0
        or not isinstance(name, str)
        or not name
        or not (guild_id is None or (type(guild_id) is int and guild_id > 0))
    ):
        raise DiscordWebhookConfigurationError(
            "The stored Discord output is invalid. Reconnect the Discord output."
        )
    return DiscordWebhookCredentials(
        url=url,
        webhook_id=webhook_id,
        channel_id=channel_id,
        guild_id=guild_id,
        name=name,
    )


def discord_webhook_status(
    *,
    paths: DiscordWebhookPaths | None = None,
) -> DiscordWebhookStatus:
    selected_paths = paths or DiscordWebhookPaths.default()
    if not selected_paths.credentials.exists():
        return DiscordWebhookStatus(connected=False)
    credentials = load_discord_webhook_credentials(paths=selected_paths)
    return DiscordWebhookStatus(
        connected=True,
        name=credentials.name,
        channel_id=credentials.channel_id,
        guild_id=credentials.guild_id,
    )


def _parse_webhook_url(url: str) -> tuple[str, str]:
    """Return the (host, path) of a syntactically valid Discord webhook URL."""
    if not isinstance(url, str) or not url:
        raise DiscordWebhookConfigurationError("The Discord webhook URL is missing.")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL is not a valid URL."
        ) from exc

    if parsed.scheme != "https" or parsed.hostname not in DISCORD_WEBHOOK_HOSTS:
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL must be an https URL on discord.com."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL has an invalid port."
        ) from exc
    if port is not None and port != 443:
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL must use the standard https port."
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL must not carry credentials or parameters."
        )
    if not DISCORD_WEBHOOK_PATH_PATTERN.fullmatch(parsed.path):
        raise DiscordWebhookConfigurationError(
            "The Discord webhook URL has an invalid format. Copy it from "
            "Channel Settings -> Integrations -> Webhooks."
        )
    return parsed.hostname, parsed.path


def _snowflake(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        if parsed > 0:
            return parsed
    return None


def _save_credentials(
    paths: DiscordWebhookPaths,
    credentials: DiscordWebhookCredentials,
) -> None:
    try:
        atomic_write_json(paths.credentials, credentials.as_json())
    except StorageError as exc:
        raise DiscordWebhookConfigurationError(
            "Could not securely save the Discord output connection."
        ) from exc


def _discord_webhook_api_error(
    raw_response: bytes,
    *,
    status: int,
) -> DiscordWebhookAPIError:
    description = ""
    retry_after: int | None = None
    try:
        data = json.loads(raw_response)
    except (UnicodeError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        raw_message = data.get("message")
        if isinstance(raw_message, str):
            description = raw_message
        raw_retry_after = data.get("retry_after")
        if isinstance(raw_retry_after, int | float) and not isinstance(
            raw_retry_after,
            bool,
        ):
            retry_after = max(1, int(raw_retry_after + 0.999))
    safe_description = " ".join(description.split())
    suffix = f": {safe_description}" if safe_description else ""
    return DiscordWebhookAPIError(
        f"Discord API request failed (HTTP {status}){suffix}.",
        status=status,
        retry_after=retry_after,
    )


def _long_message_request(text: str) -> tuple[bytes, str]:
    """Build one multipart request containing a preview and the full message."""
    preview = text[:DISCORD_LONG_MESSAGE_PREVIEW_SIZE].rstrip()
    note = f"…\n\nFull message attached as {DISCORD_ATTACHMENT_FILENAME}."
    content = f"{preview}\n\n{note}" if preview else note
    if len(content) > DISCORD_MESSAGE_LIMIT:
        # The conservative preview limit should keep this unreachable, but do not
        # let a future wording change violate Discord's content limit.
        content = note

    payload = {
        "allowed_mentions": SUPPRESSED_MENTIONS,
        "attachments": [
            {
                "description": "Full forwarded Agent Sherlock message",
                "filename": DISCORD_ATTACHMENT_FILENAME,
                "id": 0,
            }
        ],
        "content": content,
        "flags": SUPPRESS_EMBEDS_FLAG,
    }
    boundary = f"agent-sherlock-{secrets.token_hex(16)}"
    payload_json = json.dumps(payload, separators=(",", ":")).encode()
    file_content = text.encode("utf-8")
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="payload_json"\r\n',
            b"Content-Type: application/json\r\n\r\n",
            payload_json,
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="files[0]"; '
                f'filename="{DISCORD_ATTACHMENT_FILENAME}"\r\n'
            ).encode(),
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
            file_content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return body, f"multipart/form-data; boundary={boundary}"
