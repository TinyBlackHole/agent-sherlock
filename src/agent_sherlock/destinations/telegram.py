from __future__ import annotations

from agent_sherlock.integrations.telegram import (
    TelegramClient,
    TelegramCredentials,
    load_telegram_credentials,
)


class TelegramDestination:
    """Deliver Sherlock output to the single authorized Telegram chat."""

    name = "telegram"

    def __init__(
        self,
        credentials: TelegramCredentials,
        *,
        client: TelegramClient | None = None,
    ):
        self.credentials = credentials
        self.client = client or TelegramClient(credentials.token)

    @classmethod
    def open(cls) -> TelegramDestination:
        return cls(load_telegram_credentials())

    def send(self, text: str) -> None:
        self.client.send_message(self.credentials.chat_id, text)

    def send_important(self, text: str, *, discord_user_id: str) -> None:
        # Importance mentions are a Discord-only feature. Telegram still receives
        # the processed message normally if it is the selected destination.
        del discord_user_id
        self.send(text)
