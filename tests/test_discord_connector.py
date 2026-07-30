from datetime import UTC, datetime
from types import SimpleNamespace

from agent_sherlock.connectors.discord import DiscordConnector
from agent_sherlock.integrations.discord import DiscordCredentials


def credentials():
    return DiscordCredentials(
        token="discord-bot-token-with-enough-characters",
        bot_id=42,
        bot_username="sherlock_bot",
        guild_id=8,
        channel_id=9,
        channel_name="alerts",
    )


def message(*, author_id=7, channel_id=9, guild_id=8):
    return SimpleNamespace(
        id=100,
        author=SimpleNamespace(
            id=author_id,
            display_name="Ada",
            global_name="Ada",
            name="ada_dev",
        ),
        channel=SimpleNamespace(id=channel_id, name="alerts"),
        guild=SimpleNamespace(id=guild_id),
        content="Service is down",
        attachments=[
            SimpleNamespace(filename="trace.txt", url="https://cdn.example/trace")
        ],
        stickers=[SimpleNamespace(name="Alarm")],
        embeds=[
            SimpleNamespace(
                title="Incident",
                description="Investigating",
                url="https://status.example",
            )
        ],
        created_at=datetime(2026, 7, 30, 10, 0, tzinfo=UTC),
        jump_url="https://discord.com/channels/8/9/100",
    )


def test_discord_connector_normalizes_message_content():
    normalized = DiscordConnector(credentials()).normalize(message())

    assert normalized is not None
    assert normalized.key == ("discord", "8", "100")
    assert normalized.conversation_id == "9"
    assert normalized.sender == "Ada (@ada_dev)"
    assert normalized.subject == "#alerts"
    assert "Service is down" in normalized.body
    assert "Attachment: trace.txt" in normalized.body
    assert "https://cdn.example/trace" in normalized.body
    assert "Sticker: Alarm" in normalized.body
    assert "Incident" in normalized.body
    assert normalized.metadata == {
        "author_id": "7",
        "channel_id": "9",
        "guild_id": "8",
        "jump_url": "https://discord.com/channels/8/9/100",
    }


def test_discord_connector_ignores_other_channels_guilds_and_itself():
    connector = DiscordConnector(credentials())

    assert connector.normalize(message(channel_id=99)) is None
    assert connector.normalize(message(guild_id=88)) is None
    assert connector.normalize(message(author_id=42)) is None


def test_discord_connector_ignores_system_messages_and_accepts_replies():
    connector = DiscordConnector(credentials())
    system_message = message()
    system_message.type = SimpleNamespace(value=7)
    reply = message()
    reply.type = SimpleNamespace(value=19)

    assert connector.normalize(system_message) is None
    assert connector.normalize(reply) is not None


def test_discord_connector_explains_empty_content():
    event = message()
    event.content = ""
    event.attachments = []
    event.stickers = []
    event.embeds = []

    normalized = DiscordConnector(credentials()).normalize(event)

    assert normalized is not None
    assert "Message Content Intent" in normalized.body
