import json
import os
import stat

import pytest

from agent_sherlock.integrations import gmail


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeHistory:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.responses.pop(0))


class FakeMessages:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return FakeRequest(self.responses[kwargs["id"]])


class FakeUsers:
    def __init__(self, *, profile, history=(), messages=None):
        self.profile_response = profile
        self.history_resource = FakeHistory(history)
        self.messages_resource = FakeMessages(messages or {})
        self.profile_calls = []

    def getProfile(self, **kwargs):
        self.profile_calls.append(kwargs)
        return FakeRequest(self.profile_response)

    def history(self):
        return self.history_resource

    def messages(self):
        return self.messages_resource


class FakeService:
    def __init__(self, *, profile, history=(), messages=None):
        self.users_resource = FakeUsers(
            profile=profile,
            history=history,
            messages=messages,
        )

    def users(self):
        return self.users_resource


class FakeHTTPError(Exception):
    def __init__(self, status, content=None):
        super().__init__(f"HTTP {status}")
        self.resp = type("Response", (), {"status": status})()
        self.content = content


def private_paths(tmp_path):
    return gmail.GmailPaths(tmp_path / "gmail")


def write_state(paths, history_id="100", email="person@example.com"):
    gmail._save_state(
        paths,
        gmail.GmailState(history_id=history_id, email_address=email),
    )


def message_response(
    message_id,
    *,
    sender="sender@example.com",
    subject="Hello",
    date="Thu, 30 Jul 2026 10:00:00 +0000",
    internal_date="1",
):
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": internal_date,
        "payload": {
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": date},
            ]
        },
    }


def test_mailbox_reads_profile():
    service = FakeService(
        profile={"emailAddress": "person@example.com", "historyId": "123"}
    )

    profile = gmail.GmailMailbox(service).profile()

    assert profile == gmail.GmailProfile("person@example.com", "123")
    assert service.users_resource.profile_calls == [{"userId": "me"}]


def test_new_message_ids_are_incremental_paginated_and_deduplicated():
    service = FakeService(
        profile={},
        history=[
            {
                "historyId": "110",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "labelIds": ["INBOX"]}},
                            {"message": {"id": "archived", "labelIds": ["IMPORTANT"]}},
                        ]
                    }
                ],
                "nextPageToken": "page-2",
            },
            {
                "historyId": "120",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "labelIds": ["INBOX"]}},
                            {"message": {"id": "m2", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
            },
        ],
    )

    ids, history_id = gmail.GmailMailbox(service).new_message_ids("100")

    assert ids == ["m1", "m2"]
    assert history_id == "120"
    calls = service.users_resource.history_resource.calls
    assert calls[0] == {
        "userId": "me",
        "startHistoryId": "100",
        "historyTypes": ["messageAdded"],
        "labelId": "INBOX",
        "maxResults": 500,
    }
    assert calls[1]["pageToken"] == "page-2"


@pytest.mark.parametrize(
    "reason",
    [
        "rateLimitExceeded",
        "userRateLimitExceeded",
    ],
)
def test_gmail_forbidden_rate_limit_is_retryable(reason):
    error_body = json.dumps(
        {
            "error": {
                "code": 403,
                "errors": [{"reason": reason}],
            }
        }
    ).encode()

    with pytest.raises(gmail.GmailAPIError) as exc_info:
        gmail._execute(
            FakeRequest(FakeHTTPError(403, error_body)),
            "read mailbox history",
        )

    assert exc_info.value.status == 403
    assert exc_info.value.reasons == frozenset({reason})
    assert exc_info.value.retryable is True


def test_gmail_forbidden_permission_error_is_not_retryable():
    error_body = json.dumps(
        {
            "error": {
                "code": 403,
                "errors": [{"reason": "insufficientPermissions"}],
            }
        }
    ).encode()

    with pytest.raises(gmail.GmailAPIError) as exc_info:
        gmail._execute(
            FakeRequest(FakeHTTPError(403, error_body)),
            "read mailbox history",
        )

    assert exc_info.value.reasons == frozenset({"insufficientPermissions"})
    assert exc_info.value.retryable is False


def test_first_fetch_saves_current_history_as_baseline(tmp_path):
    paths = private_paths(tmp_path)
    service = FakeService(
        profile={"emailAddress": "person@example.com", "historyId": "123"}
    )

    result = gmail.fetch_new_gmail_messages(
        gmail.GmailMailbox(service),
        paths=paths,
    )

    assert result == gmail.GmailFetchResult(initialized=True)
    assert json.loads(paths.state.read_text()) == {
        "email_address": "person@example.com",
        "history_id": "123",
        "schema_version": 1,
    }


def test_fetch_returns_metadata_and_advances_history_after_success(tmp_path):
    paths = private_paths(tmp_path)
    write_state(paths)
    service = FakeService(
        profile={},
        history=[
            {
                "historyId": "120",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "newer", "labelIds": ["INBOX"]}},
                            {"message": {"id": "older", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
            }
        ],
        messages={
            "newer": message_response("newer", internal_date="20"),
            "older": message_response(
                "older",
                subject="=?utf-8?q?Ol=C3=A1?=",
                internal_date="10",
            ),
        },
    )
    mailbox = gmail.GmailMailbox(service)

    result = gmail.fetch_new_gmail_messages(mailbox, paths=paths)

    assert [message.message_id for message in result.messages] == ["older", "newer"]
    assert result.messages[0].subject == "Olá"
    assert json.loads(paths.state.read_text())["history_id"] == "120"
    for call in service.users_resource.messages_resource.calls:
        assert call["format"] == "metadata"
        assert call["metadataHeaders"] == ["Date", "From", "Subject"]


def test_fetch_does_not_advance_state_if_message_fetch_fails(tmp_path):
    paths = private_paths(tmp_path)
    write_state(paths)
    service = FakeService(
        profile={},
        history=[
            {
                "historyId": "120",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "m1", "labelIds": ["INBOX"]}}
                        ]
                    }
                ],
            }
        ],
        messages={"m1": FakeHTTPError(500)},
    )

    with pytest.raises(gmail.GmailAPIError, match="HTTP 500"):
        gmail.fetch_new_gmail_messages(gmail.GmailMailbox(service), paths=paths)

    assert json.loads(paths.state.read_text())["history_id"] == "100"


def test_fetch_skips_message_removed_during_sync(tmp_path):
    paths = private_paths(tmp_path)
    write_state(paths)
    service = FakeService(
        profile={},
        history=[
            {
                "historyId": "120",
                "history": [
                    {
                        "messagesAdded": [
                            {"message": {"id": "gone", "labelIds": ["INBOX"]}},
                            {"message": {"id": "present", "labelIds": ["INBOX"]}},
                        ]
                    }
                ],
            }
        ],
        messages={
            "gone": FakeHTTPError(404),
            "present": message_response("present"),
        },
    )

    result = gmail.fetch_new_gmail_messages(
        gmail.GmailMailbox(service),
        paths=paths,
    )

    assert [message.message_id for message in result.messages] == ["present"]
    assert json.loads(paths.state.read_text())["history_id"] == "120"


def test_expired_history_resets_baseline_without_replaying_inbox(tmp_path):
    paths = private_paths(tmp_path)
    write_state(paths)
    service = FakeService(
        profile={"emailAddress": "person@example.com", "historyId": "999"},
        history=[FakeHTTPError(404)],
    )

    result = gmail.fetch_new_gmail_messages(
        gmail.GmailMailbox(service),
        paths=paths,
    )

    assert result == gmail.GmailFetchResult(history_reset=True)
    assert json.loads(paths.state.read_text())["history_id"] == "999"


def test_old_prototype_state_is_migrated_to_history_baseline(tmp_path):
    paths = private_paths(tmp_path)
    paths.directory.mkdir(parents=True)
    paths.state.write_text('{"seen_message_ids": ["old"]}')
    service = FakeService(
        profile={"emailAddress": "person@example.com", "historyId": "222"}
    )

    result = gmail.fetch_new_gmail_messages(
        gmail.GmailMailbox(service),
        paths=paths,
    )

    assert result.initialized is True
    assert json.loads(paths.state.read_text())["history_id"] == "222"


def test_client_config_requires_google_desktop_endpoints(tmp_path):
    config = tmp_path / "client.json"
    config.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client.apps.googleusercontent.com",
                    "auth_uri": "https://attacker.example/authorize",
                    "token_uri": gmail.GOOGLE_OAUTH_EXCHANGE_URI,
                }
            }
        )
    )

    with pytest.raises(gmail.GmailConfigurationError, match="untrusted"):
        gmail._read_client_config(config)


def test_connect_saves_only_private_token_and_state(monkeypatch, tmp_path):
    paths = private_paths(tmp_path)
    client_file = tmp_path / "client.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client.apps.googleusercontent.com",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": gmail.GOOGLE_OAUTH_EXCHANGE_URI,
                }
            }
        )
    )

    class FakeCredentials:
        def to_json(self):
            return json.dumps(
                {
                    "token": "access-token",
                    "refresh_token": "refresh-token",
                    "scopes": list(gmail.GMAIL_SCOPES),
                }
            )

    service = FakeService(
        profile={"emailAddress": "person@example.com", "historyId": "123"}
    )
    monkeypatch.setattr(gmail, "_authorize", lambda _config: FakeCredentials())
    monkeypatch.setattr(gmail, "_build_service", lambda _credentials: service)

    profile = gmail.connect_gmail(client_file, paths=paths)

    assert profile.email_address == "person@example.com"
    assert paths.token.exists()
    assert paths.state.exists()
    assert not (paths.directory / "credentials.json").exists()
    if os.name == "posix":
        assert stat.S_IMODE(paths.token.stat().st_mode) == 0o600
        assert stat.S_IMODE(paths.state.stat().st_mode) == 0o600


def test_failed_authorization_preserves_existing_connection(monkeypatch, tmp_path):
    paths = private_paths(tmp_path)
    gmail.atomic_write_json(paths.token, {"old": "token"})
    client_file = tmp_path / "client.json"
    client_file.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "client.apps.googleusercontent.com",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": gmail.GOOGLE_OAUTH_EXCHANGE_URI,
                }
            }
        )
    )

    def reject(_config):
        raise gmail.GmailAuthenticationError("cancelled")

    monkeypatch.setattr(gmail, "_authorize", reject)

    with pytest.raises(gmail.GmailAuthenticationError):
        gmail.connect_gmail(client_file, paths=paths)

    assert json.loads(paths.token.read_text()) == {"old": "token"}


def test_status_rejects_token_with_broader_legacy_scope(tmp_path):
    paths = private_paths(tmp_path)
    gmail.atomic_write_json(
        paths.token,
        {"scopes": ["https://www.googleapis.com/auth/gmail.readonly"]},
    )

    with pytest.raises(gmail.GmailAuthenticationError, match="outdated permissions"):
        gmail.gmail_status(paths=paths)
