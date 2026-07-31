from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.client import HTTPException, HTTPSConnection
from pathlib import Path
from typing import Any
from urllib.parse import quote

from agent_sherlock.storage import (
    StorageError,
    atomic_write_json,
    config_root,
    read_json_object,
)

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_TOKEN_PATTERN = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,}$")
TELEGRAM_MESSAGE_LIMIT = 4_096
TELEGRAM_SAFE_CHUNK_SIZE = 4_000
TELEGRAM_CAPTION_LIMIT = 1_024
TELEGRAM_LONG_MESSAGE_PREVIEW_SIZE = 800
TELEGRAM_ATTACHMENT_FILENAME = "sherlock-message.txt"
TELEGRAM_CONNECTION_TIMEOUT_SECONDS = 120.0
TELEGRAM_EMPTY_MESSAGE_TEXT = "(empty message)"
MAX_TELEGRAM_API_RESPONSE_BYTES = 1_000_000


class TelegramError(RuntimeError):
    """Base class for expected Telegram integration failures."""


class TelegramConfigurationError(TelegramError):
    """Raised when local Telegram configuration is missing or invalid."""


class TelegramAuthenticationError(TelegramError):
    """Raised when a bot token or chat authorization is rejected."""


class TelegramAPIError(TelegramError):
    """Raised when Telegram returns an API or transport failure."""

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
        return (
            self.status is None
            or self.status == 429
            or (self.status is not None and self.status >= 500)
        )


@dataclass(frozen=True, slots=True)
class TelegramPaths:
    directory: Path

    @classmethod
    def default(cls) -> TelegramPaths:
        return cls(config_root() / "connections" / "telegram")

    @property
    def credentials(self) -> Path:
        return self.directory / "token.json"


@dataclass(frozen=True, slots=True)
class TelegramProfile:
    bot_id: int
    username: str


@dataclass(frozen=True, slots=True)
class TelegramAuthorization:
    bot_username: str
    url: str


@dataclass(frozen=True, slots=True)
class TelegramCredentials:
    # The token is a bearer secret: keep it out of reprs, logs, and tracebacks.
    token: str = field(repr=False)
    chat_id: int
    bot_id: int
    bot_username: str

    def as_json(self) -> dict[str, Any]:
        return {
            "bot_id": self.bot_id,
            "bot_username": self.bot_username,
            "chat_id": self.chat_id,
            "token": self.token,
        }


@dataclass(frozen=True, slots=True)
class TelegramStatus:
    connected: bool
    bot_username: str = ""
    chat_id: int | None = None


class TelegramClient:
    """Small synchronous client for the Telegram Bot API."""

    def __init__(self, token: str):
        _validate_token(token)
        self._token = token

    def get_me(self) -> TelegramProfile:
        result = self._request("getMe")
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram returned an invalid bot profile.")
        bot_id = result.get("id")
        username = result.get("username")
        if type(bot_id) is not int or not isinstance(username, str) or not username:
            raise TelegramAPIError("Telegram returned an incomplete bot profile.")
        return TelegramProfile(bot_id=bot_id, username=username)

    def get_chat(self, chat_id: int) -> dict[str, Any]:
        result = self._request("getChat", {"chat_id": chat_id})
        if not isinstance(result, dict):
            raise TelegramAPIError("Telegram returned an invalid chat.")
        return result

    def get_updates(
        self,
        *,
        offset: int | None = None,
        timeout: int = 0,
    ) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {
            "allowed_updates": ["message"],
            "timeout": timeout,
        }
        if offset is not None:
            payload["offset"] = offset
        result = self._request(
            "getUpdates",
            payload,
            timeout=max(timeout + 5, 10),
        )
        if not isinstance(result, list) or not all(
            isinstance(update, dict) for update in result
        ):
            raise TelegramAPIError("Telegram returned invalid bot updates.")
        return result

    def send_message(self, chat_id: int, text: str) -> None:
        """Deliver one message using exactly one API request.

        Splitting long text across several sendMessage calls made delivery
        non-atomic: a failure on the second request re-sent the first one on the
        next retry, so the recipient saw part of the message twice. Anything too
        long for a single message is uploaded as one attachment instead.
        """
        content = text or TELEGRAM_EMPTY_MESSAGE_TEXT
        if _utf16_length(content) <= TELEGRAM_SAFE_CHUNK_SIZE:
            self._request(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "disable_web_page_preview": True,
                    "text": content,
                },
            )
            return
        body, content_type = _long_message_request(chat_id, content)
        self._request("sendDocument", body=body, content_type=content_type)

    def _request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: int = 30,
        body: bytes | None = None,
        content_type: str = "application/json",
    ) -> Any:
        request_body = (
            body
            if body is not None
            else json.dumps(payload or {}, separators=(",", ":")).encode()
        )
        connection = HTTPSConnection(TELEGRAM_API_HOST, timeout=timeout)
        try:
            connection.request(
                "POST",
                f"/bot{self._token}/{method}",
                body=request_body,
                headers={
                    "Content-Type": content_type,
                    "User-Agent": "Agent-Sherlock",
                },
            )
            response = connection.getresponse()
            raw_response = response.read(MAX_TELEGRAM_API_RESPONSE_BYTES + 1)
            status = response.status
        except (OSError, HTTPException) as exc:
            raise TelegramAPIError("Cannot reach the Telegram API.") from exc
        finally:
            connection.close()

        if len(raw_response) > MAX_TELEGRAM_API_RESPONSE_BYTES:
            raise TelegramAPIError(
                "Telegram returned an unexpectedly large API response.",
                status=status,
            )
        try:
            response_data = json.loads(raw_response)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TelegramAPIError(
                "Telegram returned an unreadable API response.",
                status=status,
            ) from exc
        if not isinstance(response_data, dict):
            raise TelegramAPIError(
                "Telegram returned an invalid API response.",
                status=status,
            )
        if not 200 <= status < 300 or response_data.get("ok") is not True:
            raise _telegram_api_error(raw_response, status=status)
        return response_data.get("result")


def connect_telegram(
    token: str,
    *,
    chat_id: int | None = None,
    paths: TelegramPaths | None = None,
    on_authorization: Callable[[TelegramAuthorization], None] | None = None,
    timeout: float = TELEGRAM_CONNECTION_TIMEOUT_SECONDS,
    client_factory: Callable[[str], TelegramClient] = TelegramClient,
) -> TelegramStatus:
    selected_paths = paths or TelegramPaths.default()
    client = client_factory(token)
    try:
        profile = client.get_me()
    except TelegramAPIError as exc:
        if exc.status in {401, 404}:
            raise TelegramAuthenticationError(
                "Telegram rejected the bot token."
            ) from exc
        raise

    selected_chat_id = chat_id
    if selected_chat_id is not None:
        chat = client.get_chat(selected_chat_id)
        if (
            chat.get("type") != "private"
            or type(chat.get("id")) is not int
            or chat.get("id") != selected_chat_id
        ):
            raise TelegramConfigurationError(
                "Sherlock's Telegram destination must be a private bot chat."
            )
    else:
        selected_chat_id = _authorize_private_chat(
            client,
            profile,
            on_authorization=on_authorization,
            timeout=timeout,
        )

    client.send_message(
        selected_chat_id,
        "Agent Sherlock connected. New messages will be delivered in this chat.",
    )
    credentials = TelegramCredentials(
        token=token,
        chat_id=selected_chat_id,
        bot_id=profile.bot_id,
        bot_username=profile.username,
    )
    _save_credentials(selected_paths, credentials)
    return TelegramStatus(
        connected=True,
        bot_username=profile.username,
        chat_id=selected_chat_id,
    )


def load_telegram_credentials(
    *,
    paths: TelegramPaths | None = None,
) -> TelegramCredentials:
    selected_paths = paths or TelegramPaths.default()
    if not selected_paths.credentials.exists():
        raise TelegramAuthenticationError(
            "Telegram is not connected. Run `sherlock output telegram connect`."
        )
    try:
        data = read_json_object(selected_paths.credentials, private=True)
    except StorageError as exc:
        raise TelegramConfigurationError(str(exc)) from exc

    token = data.get("token")
    chat_id = data.get("chat_id")
    bot_id = data.get("bot_id")
    bot_username = data.get("bot_username")
    if not isinstance(token, str):
        raise TelegramConfigurationError(
            "The stored Telegram bot token is invalid. Reconnect Telegram."
        )
    try:
        _validate_token(token)
    except TelegramAuthenticationError as exc:
        raise TelegramConfigurationError(
            "The stored Telegram bot token is invalid. Reconnect Telegram."
        ) from exc
    if (
        type(chat_id) is not int
        or type(bot_id) is not int
        or not isinstance(bot_username, str)
        or not bot_username
    ):
        raise TelegramConfigurationError(
            "The stored Telegram connection is invalid. Reconnect Telegram."
        )
    return TelegramCredentials(
        token=token,
        chat_id=chat_id,
        bot_id=bot_id,
        bot_username=bot_username,
    )


def telegram_status(*, paths: TelegramPaths | None = None) -> TelegramStatus:
    selected_paths = paths or TelegramPaths.default()
    if not selected_paths.credentials.exists():
        return TelegramStatus(connected=False)
    credentials = load_telegram_credentials(paths=selected_paths)
    return TelegramStatus(
        connected=True,
        bot_username=credentials.bot_username,
        chat_id=credentials.chat_id,
    )


def _authorize_private_chat(
    client: TelegramClient,
    profile: TelegramProfile,
    *,
    on_authorization: Callable[[TelegramAuthorization], None] | None,
    timeout: float,
) -> int:
    baseline_updates = client.get_updates()
    offset = _next_update_offset(baseline_updates)
    nonce = secrets.token_urlsafe(18)
    authorization = TelegramAuthorization(
        bot_username=profile.username,
        url=f"https://t.me/{quote(profile.username)}?start={quote(nonce)}",
    )
    if on_authorization is not None:
        on_authorization(authorization)

    deadline = time.monotonic() + timeout
    expected_commands = {
        f"/start {nonce}",
        f"/start@{profile.username} {nonce}",
    }
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        updates = client.get_updates(
            offset=offset,
            timeout=max(0, min(10, int(remaining))),
        )
        next_offset = _next_update_offset(updates)
        if next_offset is not None:
            offset = next_offset
        for update in updates:
            chat_id = _authorized_chat_id(update, expected_commands)
            if chat_id is not None:
                return chat_id
        if not updates:
            time.sleep(min(0.2, max(remaining, 0)))

    raise TelegramAuthenticationError(
        "Telegram authorization timed out. Run connect again and open the new link."
    )


def _authorized_chat_id(
    update: dict[str, Any],
    expected_commands: set[str],
) -> int | None:
    message = update.get("message")
    if not isinstance(message, dict) or message.get("text") not in expected_commands:
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        return None
    chat_id = chat.get("id")
    sender_id = sender.get("id")
    if chat.get("type") != "private" or type(chat_id) is not int:
        return None
    return chat_id if sender_id == chat_id else None


def _next_update_offset(updates: list[dict[str, Any]]) -> int | None:
    update_ids = [
        update_id
        for update in updates
        if type(update_id := update.get("update_id")) is int
    ]
    return max(update_ids) + 1 if update_ids else None


def _save_credentials(paths: TelegramPaths, credentials: TelegramCredentials) -> None:
    try:
        atomic_write_json(paths.credentials, credentials.as_json())
    except StorageError as exc:
        raise TelegramConfigurationError(
            "Could not securely save the Telegram connection."
        ) from exc


def _validate_token(token: str) -> None:
    if not TELEGRAM_TOKEN_PATTERN.fullmatch(token):
        raise TelegramAuthenticationError(
            "The Telegram bot token has an invalid format."
        )


def _telegram_api_error(raw_response: bytes, *, status: int) -> TelegramAPIError:
    description = ""
    retry_after: int | None = None
    try:
        data = json.loads(raw_response)
    except (UnicodeError, json.JSONDecodeError):
        data = {}
    if isinstance(data, dict):
        error_code = data.get("error_code")
        if isinstance(error_code, int):
            status = error_code
        raw_description = data.get("description")
        if isinstance(raw_description, str):
            description = raw_description
        parameters = data.get("parameters")
        if isinstance(parameters, dict):
            raw_retry_after = parameters.get("retry_after")
            if isinstance(raw_retry_after, int):
                retry_after = raw_retry_after
    safe_description = " ".join(description.split())
    suffix = f": {safe_description}" if safe_description else ""
    return TelegramAPIError(
        f"Telegram API request failed (HTTP {status}){suffix}.",
        status=status,
        retry_after=retry_after,
    )


def _long_message_request(chat_id: int, text: str) -> tuple[bytes, str]:
    """Build one multipart sendDocument request holding the whole message."""
    preview_end = _utf16_prefix_index(
        text,
        max_units=TELEGRAM_LONG_MESSAGE_PREVIEW_SIZE,
    )
    preview = text[:preview_end].rstrip()
    note = f"…\n\nFull message attached as {TELEGRAM_ATTACHMENT_FILENAME}."
    caption = f"{preview}\n\n{note}" if preview else note
    if _utf16_length(caption) > TELEGRAM_CAPTION_LIMIT:
        # The conservative preview size should keep this unreachable, but a
        # future wording change must not violate Telegram's caption limit.
        caption = note

    boundary = f"agent-sherlock-{secrets.token_hex(16)}"
    fields = {
        "caption": caption,
        "chat_id": str(chat_id),
    }
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.extend(
            (
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            )
        )
    parts.extend(
        (
            f"--{boundary}\r\n".encode(),
            (
                'Content-Disposition: form-data; name="document"; '
                f'filename="{TELEGRAM_ATTACHMENT_FILENAME}"\r\n'
            ).encode(),
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n",
            text.encode("utf-8"),
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _utf16_length(value: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in value)


def _utf16_prefix_index(value: str, *, max_units: int) -> int:
    units = 0
    for index, character in enumerate(value):
        character_units = 2 if ord(character) > 0xFFFF else 1
        if units + character_units > max_units:
            return max(1, index)
        units += character_units
    return len(value)
