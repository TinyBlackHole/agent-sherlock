from __future__ import annotations

from agent_sherlock.integrations.discord_webhook import (
    DiscordWebhookClient,
    DiscordWebhookCredentials,
    load_discord_webhook_credentials,
)


class DiscordDestination:
    """Deliver Sherlock output to the single configured Discord webhook."""

    name = "discord"

    def __init__(
        self,
        credentials: DiscordWebhookCredentials,
        *,
        client: DiscordWebhookClient | None = None,
    ):
        self.credentials = credentials
        self.client = client or DiscordWebhookClient(credentials.url)

    @classmethod
    def open(cls) -> DiscordDestination:
        return cls(load_discord_webhook_credentials())

    def send(self, text: str) -> None:
        self.client.send_message(text)

    def send_important(self, text: str, *, discord_user_id: str) -> None:
        self.client.send_message(text, mention_user_id=discord_user_id)
