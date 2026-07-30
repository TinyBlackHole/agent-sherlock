from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_sherlock.domain import InboundMessage
from agent_sherlock.integrations.discord import DiscordCredentials

SUPPORTED_MESSAGE_TYPES = frozenset({0, 19})


class DiscordConnector:
    """Normalize live messages from one configured Discord server channel."""

    name = "discord"

    def __init__(self, credentials: DiscordCredentials):
        self.credentials = credentials

    def normalize(self, message: Any) -> InboundMessage | None:
        channel = getattr(message, "channel", None)
        author = getattr(message, "author", None)
        guild = getattr(message, "guild", None)
        if (
            getattr(channel, "id", None) != self.credentials.channel_id
            or getattr(guild, "id", None) != self.credentials.guild_id
            or getattr(author, "id", None) == self.credentials.bot_id
            or not _is_supported_message_type(message)
        ):
            return None

        message_id = getattr(message, "id", None)
        author_id = getattr(author, "id", None)
        if type(message_id) is not int or type(author_id) is not int:
            return None

        received_at = getattr(message, "created_at", None)
        if not isinstance(received_at, datetime):
            received_at = datetime.now(UTC)
        elif received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=UTC)

        channel_name = _first_text(
            getattr(channel, "name", None),
            self.credentials.channel_name,
        )
        return InboundMessage(
            source="discord",
            account_id=str(self.credentials.guild_id),
            external_id=str(message_id),
            conversation_id=str(self.credentials.channel_id),
            sender=_sender_name(author),
            subject=f"#{channel_name}",
            body=_message_body(message),
            received_at=received_at,
            metadata={
                "author_id": str(author_id),
                "channel_id": str(self.credentials.channel_id),
                "guild_id": str(self.credentials.guild_id),
                "jump_url": _first_text(getattr(message, "jump_url", None)),
            },
        )


def _sender_name(author: Any) -> str:
    display_name = _first_text(
        getattr(author, "display_name", None),
        getattr(author, "global_name", None),
        getattr(author, "name", None),
    )
    username = _first_text(getattr(author, "name", None))
    if display_name and username and display_name != username:
        return f"{display_name} (@{username})"
    return display_name or username


def _message_body(message: Any) -> str:
    sections: list[str] = []
    content = getattr(message, "content", None)
    if isinstance(content, str) and content.strip():
        sections.append(content.strip())

    for attachment in _sequence(getattr(message, "attachments", ())):
        filename = _first_text(getattr(attachment, "filename", None), "attachment")
        url = _first_text(getattr(attachment, "url", None))
        label = f"Attachment: {filename}"
        sections.append(f"{label}\n{url}" if url else label)

    for sticker in _sequence(getattr(message, "stickers", ())):
        name = _first_text(getattr(sticker, "name", None), "sticker")
        sections.append(f"Sticker: {name}")

    for embed in _sequence(getattr(message, "embeds", ())):
        title = _first_text(getattr(embed, "title", None))
        description = _first_text(getattr(embed, "description", None))
        url = _first_text(getattr(embed, "url", None))
        details = "\n".join(value for value in (title, description, url) if value)
        if details:
            sections.append(f"Embed:\n{details}")

    if sections:
        return "\n\n".join(sections)
    return (
        "(message without readable content; verify that Message Content Intent "
        "is enabled)"
    )


def _is_supported_message_type(message: Any) -> bool:
    message_type = getattr(message, "type", None)
    if message_type is None:
        return True
    value = getattr(message_type, "value", message_type)
    return type(value) is int and value in SUPPORTED_MESSAGE_TYPES


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, (list, tuple)):
        return tuple(value)
    return ()


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
