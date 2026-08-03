import json
import os
import stat

import pytest

from agent_sherlock.integrations import discord_webhook

WEBHOOK_ID = "123456789012345678"
WEBHOOK_TOKEN = "a" * 60
WEBHOOK_URL = f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}"


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self):
        return self._body


class FakeConnection:
    """Captures one request/response cycle for HTTPSConnection."""

    def __init__(self, responses):
        self._responses = responses
        self.requests = []
        self.host = None
        self.closed = False

    def __call__(self, host, timeout=None):
        self.host = host
        return self

    def request(self, method, path, body=None, headers=None):
        request_headers = headers or {}
        content_type = request_headers.get("Content-Type", "")
        captured_body = (
            json.loads(body) if body and content_type == "application/json" else body
        )
        self.requests.append(
            {
                "method": method,
                "path": path,
                "body": captured_body,
                "headers": request_headers,
            }
        )

    def getresponse(self):
        return self._responses.pop(0)

    def close(self):
        self.closed = True


class FakeWebhookClient:
    def __init__(self, url):
        self.url = url
        self.sent = []

    def get_webhook(self):
        return discord_webhook.DiscordWebhookTarget(
            webhook_id=int(WEBHOOK_ID),
            channel_id=555,
            guild_id=777,
            name="sherlock-inbox",
        )

    def send_message(self, text):
        self.sent.append(text)


@pytest.mark.parametrize(
    "url",
    [
        WEBHOOK_URL,
        f"https://discord.com/api/v10/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discordapp.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://ptb.discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
    ],
)
def test_parse_webhook_url_accepts_official_discord_forms(url):
    host, path = discord_webhook._parse_webhook_url(url)

    assert host.endswith("discord.com") or host == "discordapp.com"
    assert path.endswith(f"/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "not-a-url",
        f"http://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://evil.example.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discord.com.evil.example/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discord.com:8443/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discord.com:notaport/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discord.com:99999/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://user:pass@discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true",
        "https://discord.com/api/webhooks/abc/short",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{'a' * 10}",
        f"https://discord.com/api/webhooks/{WEBHOOK_ID}/{'é' * 60}",
    ],
)
def test_parse_webhook_url_rejects_unsafe_or_malformed_urls(url):
    with pytest.raises(discord_webhook.DiscordWebhookConfigurationError):
        discord_webhook._parse_webhook_url(url)


def test_connect_discord_webhook_validates_and_saves_credentials(tmp_path):
    paths = discord_webhook.DiscordWebhookPaths(tmp_path / "discord-webhook")

    status = discord_webhook.connect_discord_webhook(
        f"  {WEBHOOK_URL}  ",
        paths=paths,
        client_factory=FakeWebhookClient,
    )

    assert status == discord_webhook.DiscordWebhookStatus(
        connected=True,
        name="sherlock-inbox",
        channel_id=555,
        guild_id=777,
    )
    assert json.loads(paths.credentials.read_text()) == {
        "channel_id": 555,
        "guild_id": 777,
        "name": "sherlock-inbox",
        "schema_version": 1,
        "url": WEBHOOK_URL,
        "webhook_id": int(WEBHOOK_ID),
    }
    if os.name == "posix":
        assert stat.S_IMODE(paths.credentials.stat().st_mode) == 0o600


def test_connect_discord_webhook_rejects_a_non_discord_url(tmp_path):
    paths = discord_webhook.DiscordWebhookPaths(tmp_path / "discord-webhook")

    with pytest.raises(discord_webhook.DiscordWebhookConfigurationError):
        discord_webhook.connect_discord_webhook(
            "https://evil.example.com/api/webhooks/1/aaaa",
            paths=paths,
            client_factory=FakeWebhookClient,
        )
    assert not paths.credentials.exists()


def test_load_discord_webhook_credentials_reports_a_missing_connection(tmp_path):
    paths = discord_webhook.DiscordWebhookPaths(tmp_path / "discord-webhook")

    with pytest.raises(discord_webhook.DiscordWebhookAuthenticationError):
        discord_webhook.load_discord_webhook_credentials(paths=paths)
    assert discord_webhook.discord_webhook_status(paths=paths) == (
        discord_webhook.DiscordWebhookStatus(connected=False)
    )


def test_load_discord_webhook_credentials_rejects_a_tampered_url(tmp_path):
    paths = discord_webhook.DiscordWebhookPaths(tmp_path / "discord-webhook")
    discord_webhook.connect_discord_webhook(
        WEBHOOK_URL,
        paths=paths,
        client_factory=FakeWebhookClient,
    )
    stored = json.loads(paths.credentials.read_text())
    stored["url"] = "https://evil.example.com/api/webhooks/1/aaaa"
    paths.credentials.write_text(json.dumps(stored))

    with pytest.raises(discord_webhook.DiscordWebhookConfigurationError):
        discord_webhook.load_discord_webhook_credentials(paths=paths)


def test_send_message_suppresses_mentions_and_embeds(monkeypatch):
    connection = FakeConnection([FakeResponse(200, b'{"id": "999"}')])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message("Hello there")

    assert connection.host == "discord.com"
    assert connection.requests == [
        {
            "method": "POST",
            "path": f"/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true",
            "body": {
                "allowed_mentions": {"parse": []},
                "content": "Hello there",
                "flags": 4,
            },
            "headers": {
                "Content-Type": "application/json",
                "User-Agent": "Agent-Sherlock",
            },
        }
    ]
    assert connection.closed


def test_send_message_mentions_only_the_explicitly_allowed_user(monkeypatch):
    connection = FakeConnection([FakeResponse(200, b'{"id": "999"}')])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message(
        "Important @everyone <@&987654321>",
        mention_user_id="123456789012345678",
    )

    payload = connection.requests[0]["body"]
    assert payload == {
        "allowed_mentions": {"users": ["123456789012345678"]},
        "content": ("<@123456789012345678>\nImportant @everyone <@&987654321>"),
        "flags": 4,
    }


def test_send_message_rejects_an_invalid_notification_user_id():
    with pytest.raises(
        discord_webhook.DiscordWebhookConfigurationError,
        match="user ID",
    ):
        discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message(
            "Important",
            mention_user_id="@everyone",
        )


def test_send_message_posts_long_text_once_with_the_full_message_attached(monkeypatch):
    connection = FakeConnection([FakeResponse(200, b'{"id": "999"}')])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)
    long_text = "\n".join(["x" * 100] * 40)

    discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message(long_text)

    assert len(connection.requests) == 1
    request = connection.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == (f"/api/webhooks/{WEBHOOK_ID}/{WEBHOOK_TOKEN}?wait=true")
    assert request["headers"]["Content-Type"].startswith(
        "multipart/form-data; boundary="
    )
    assert request["headers"]["User-Agent"] == "Agent-Sherlock"

    body = request["body"]
    assert isinstance(body, bytes)
    assert long_text.encode() in body
    assert b'name="files[0]"; filename="sherlock-message.txt"' in body
    assert b'"allowed_mentions":{"parse":[]}' in body
    assert b'"filename":"sherlock-message.txt"' in body
    assert b"Full message attached as sherlock-message.txt." in body


def test_send_message_reports_a_deleted_webhook_as_an_authentication_error(
    monkeypatch,
):
    connection = FakeConnection([FakeResponse(404, b'{"message": "Unknown Webhook"}')])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    with pytest.raises(discord_webhook.DiscordWebhookAuthenticationError):
        discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message("Hello")


def test_send_message_requires_discord_to_confirm_the_saved_message(monkeypatch):
    connection = FakeConnection([FakeResponse(204, b"")])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    with pytest.raises(discord_webhook.DiscordWebhookAPIError, match="confirm"):
        discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message("Hello")


def test_send_message_surfaces_a_retryable_rate_limit(monkeypatch):
    connection = FakeConnection(
        [
            FakeResponse(
                429, b'{"message": "You are being rate limited.", "retry_after": 1.25}'
            )
        ]
    )
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    with pytest.raises(discord_webhook.DiscordWebhookAPIError) as error:
        discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message("Hello")

    assert error.value.status == 429
    assert error.value.retry_after == 2
    assert error.value.retryable is True


def test_a_client_error_is_not_retryable(monkeypatch):
    connection = FakeConnection([FakeResponse(400, b'{"message": "Invalid body"}')])
    monkeypatch.setattr(discord_webhook, "HTTPSConnection", connection)

    with pytest.raises(discord_webhook.DiscordWebhookAPIError) as error:
        discord_webhook.DiscordWebhookClient(WEBHOOK_URL).send_message("Hello")

    assert error.value.retryable is False
