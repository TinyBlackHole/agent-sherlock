import json
import os
import stat

import pytest

from agent_sherlock.integrations import telegram

TOKEN = "123456:abcdefghijklmnopqrstuvwxyz"


class FakeTelegramClient:
    def __init__(self, *, updates=()):
        self.updates = list(updates)
        self.sent = []
        self.chat_calls = []

    def get_me(self):
        return telegram.TelegramProfile(bot_id=42, username="sherlock_bot")

    def get_chat(self, chat_id):
        self.chat_calls.append(chat_id)
        return {"id": chat_id, "type": "private"}

    def get_updates(self, **_kwargs):
        return self.updates.pop(0)

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_connect_telegram_validates_private_chat_and_saves_credentials(tmp_path):
    paths = telegram.TelegramPaths(tmp_path / "telegram")
    client = FakeTelegramClient()

    status = telegram.connect_telegram(
        TOKEN,
        chat_id=7,
        paths=paths,
        client_factory=lambda _token: client,
    )

    assert status == telegram.TelegramStatus(
        connected=True,
        bot_username="sherlock_bot",
        chat_id=7,
    )
    assert client.chat_calls == [7]
    assert client.sent[0][0] == 7
    assert json.loads(paths.credentials.read_text()) == {
        "bot_id": 42,
        "bot_username": "sherlock_bot",
        "chat_id": 7,
        "token": TOKEN,
    }
    if os.name == "posix":
        assert stat.S_IMODE(paths.credentials.stat().st_mode) == 0o600


def test_connect_telegram_authorizes_chat_with_one_time_link(
    monkeypatch,
    tmp_path,
):
    paths = telegram.TelegramPaths(tmp_path / "telegram")
    client = FakeTelegramClient(
        updates=[
            [],
            [
                {
                    "update_id": 10,
                    "message": {
                        "text": "/start secure-nonce",
                        "chat": {"id": 7, "type": "private"},
                        "from": {"id": 7},
                    },
                }
            ],
        ]
    )
    authorizations = []
    monkeypatch.setattr(
        telegram.secrets, "token_urlsafe", lambda _length: "secure-nonce"
    )

    status = telegram.connect_telegram(
        TOKEN,
        paths=paths,
        on_authorization=authorizations.append,
        client_factory=lambda _token: client,
    )

    assert status.chat_id == 7
    assert authorizations == [
        telegram.TelegramAuthorization(
            bot_username="sherlock_bot",
            url="https://t.me/sherlock_bot?start=secure-nonce",
        )
    ]


def test_connect_telegram_rejects_non_private_destination(tmp_path):
    paths = telegram.TelegramPaths(tmp_path / "telegram")
    client = FakeTelegramClient()
    client.get_chat = lambda _chat_id: {"id": 7, "type": "group"}

    with pytest.raises(telegram.TelegramConfigurationError, match="private"):
        telegram.connect_telegram(
            TOKEN,
            chat_id=7,
            paths=paths,
            client_factory=lambda _token: client,
        )

    assert not paths.credentials.exists()


def test_telegram_status_rejects_invalid_stored_token(tmp_path):
    paths = telegram.TelegramPaths(tmp_path / "telegram")
    telegram.atomic_write_json(
        paths.credentials,
        {
            "bot_id": 42,
            "bot_username": "sherlock_bot",
            "chat_id": 7,
            "token": "invalid",
        },
    )

    with pytest.raises(telegram.TelegramConfigurationError, match="token"):
        telegram.telegram_status(paths=paths)


def test_telegram_api_error_retryability():
    assert telegram.TelegramAPIError("rate limit", status=429).retryable is True
    assert telegram.TelegramAPIError("server", status=500).retryable is True
    assert telegram.TelegramAPIError("forbidden", status=403).retryable is False


def test_long_telegram_messages_are_chunked():
    chunks = telegram._message_chunks("x" * 8_500)

    assert "".join(chunks) == "x" * 8_500
    assert all(
        telegram._utf16_length(chunk) <= telegram.TELEGRAM_SAFE_CHUNK_SIZE
        for chunk in chunks
    )


def test_telegram_chunks_astral_characters_by_utf16_units():
    text = "😀" * 4_500

    chunks = telegram._message_chunks(text)

    assert "".join(chunks) == text
    assert all(
        telegram._utf16_length(chunk) <= telegram.TELEGRAM_SAFE_CHUNK_SIZE
        for chunk in chunks
    )


def test_utf16_prefix_index_always_advances_for_one_astral_character():
    assert telegram._utf16_prefix_index("😀", max_units=1) == 1


def test_send_message_requests_every_long_text_chunk(monkeypatch):
    client = telegram.TelegramClient(TOKEN)
    requests = []

    def fake_request(method, payload=None, **_kwargs):
        requests.append((method, payload))
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    text = "A" * 9_000

    client.send_message(7, text)

    expected_chunks = telegram._message_chunks(text)
    assert [payload["text"] for _, payload in requests] == list(expected_chunks)
    assert all(method == "sendMessage" for method, _ in requests)
