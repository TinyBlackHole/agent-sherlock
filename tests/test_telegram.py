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


def test_telegram_api_response_is_bounded(monkeypatch):
    requested_read_sizes = []

    class Response:
        status = 200

        def read(self, size):
            requested_read_sizes.append(size)
            return b"x" * size

    class Connection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(telegram, "HTTPSConnection", Connection)

    with pytest.raises(telegram.TelegramAPIError, match="large API response"):
        telegram.TelegramClient(TOKEN).get_me()

    assert requested_read_sizes == [telegram.MAX_TELEGRAM_API_RESPONSE_BYTES + 1]


def test_long_telegram_messages_are_sent_as_one_attachment(monkeypatch):
    client = telegram.TelegramClient(TOKEN)
    requests = []

    def fake_request(method, payload=None, **kwargs):
        requests.append((method, payload, kwargs))
        return {}

    monkeypatch.setattr(client, "_request", fake_request)
    text = "A" * 9_000

    client.send_message(7, text)

    assert len(requests) == 1
    method, payload, kwargs = requests[0]
    assert method == "sendDocument"
    assert payload is None
    assert kwargs["content_type"].startswith("multipart/form-data; boundary=")
    body = kwargs["body"]
    assert text.encode() in body
    assert b'name="chat_id"' in body
    assert telegram.TELEGRAM_ATTACHMENT_FILENAME.encode() in body


def test_long_astral_telegram_messages_are_sent_as_one_attachment(monkeypatch):
    client = telegram.TelegramClient(TOKEN)
    requests = []

    monkeypatch.setattr(
        client,
        "_request",
        lambda method, payload=None, **kwargs: requests.append(method) or {},
    )
    # 4 500 emoji stay under Telegram's character limit but exceed its UTF-16
    # unit limit, which is what actually bounds a message.
    client.send_message(7, "\N{GRINNING FACE}" * 4_500)

    assert requests == ["sendDocument"]


def test_short_telegram_messages_are_sent_as_one_plain_message(monkeypatch):
    client = telegram.TelegramClient(TOKEN)
    requests = []

    def fake_request(method, payload=None, **_kwargs):
        requests.append((method, payload))
        return {}

    monkeypatch.setattr(client, "_request", fake_request)

    client.send_message(7, "short message")

    assert requests == [
        (
            "sendMessage",
            {
                "chat_id": 7,
                "disable_web_page_preview": True,
                "text": "short message",
            },
        )
    ]


def test_telegram_attachment_caption_stays_within_the_caption_limit():
    body, _ = telegram._long_message_request(7, "line\n" * 5_000)

    caption = body.split(b'name="caption"\r\n\r\n', 1)[1].split(b"\r\n--", 1)[0]
    assert telegram._utf16_length(caption.decode()) <= telegram.TELEGRAM_CAPTION_LIMIT


def test_utf16_prefix_index_always_advances_for_one_astral_character():
    assert telegram._utf16_prefix_index("😀", max_units=1) == 1


def test_telegram_credentials_never_repr_their_token():
    credentials = telegram.TelegramCredentials(
        token=TOKEN,
        chat_id=7,
        bot_id=1,
        bot_username="sherlock_bot",
    )

    # A traceback, log line, or failing assertion must not leak the token.
    assert TOKEN not in repr(credentials)
    assert "sherlock_bot" in repr(credentials)
    assert credentials.as_json()["token"] == TOKEN
