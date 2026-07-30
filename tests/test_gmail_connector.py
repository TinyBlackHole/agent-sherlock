import json

import pytest

from agent_sherlock.connectors.gmail import GmailConnector, GmailConnectorError
from agent_sherlock.integrations.gmail import (
    GmailMessage,
    GmailPaths,
    GmailProfile,
    GmailState,
    _save_state,
)


class FakeMailbox:
    def __init__(self):
        self.message_calls = []

    def profile(self):
        return GmailProfile("person@example.com", "999")

    def new_message_ids(self, history_id):
        assert history_id == "100"
        return ["m1"], "120"

    def message(self, message_id):
        self.message_calls.append(message_id)
        return GmailMessage(
            message_id=message_id,
            thread_id="thread-1",
            sender="sender@example.com",
            subject="Hello",
            date="Thu, 30 Jul 2026 10:00:00 +0000",
            internal_date=1_722_336_000_000,
            body="Full email body",
        )


def test_gmail_connector_advances_checkpoint_only_after_acknowledgement(tmp_path):
    paths = GmailPaths(tmp_path / "gmail")
    _save_state(
        paths,
        GmailState(history_id="100", email_address="person@example.com"),
    )
    connector = GmailConnector(FakeMailbox(), paths=paths)

    batch = connector.poll()

    assert batch.checkpoint == "120"
    assert len(batch.messages) == 1
    assert batch.messages[0].source == "gmail"
    assert batch.messages[0].account_id == "person@example.com"
    assert batch.messages[0].body == "Full email body"
    assert json.loads(paths.state.read_text())["history_id"] == "100"

    connector.acknowledge(batch)

    assert json.loads(paths.state.read_text())["history_id"] == "120"


def test_gmail_connector_reuses_one_pending_batch_and_raises_typed_error(tmp_path):
    paths = GmailPaths(tmp_path / "gmail")
    _save_state(
        paths,
        GmailState(history_id="100", email_address="person@example.com"),
    )
    mailbox = FakeMailbox()
    connector = GmailConnector(mailbox, paths=paths)

    batch = connector.poll()

    assert connector.poll() is batch
    assert mailbox.message_calls == ["m1"]

    connector.acknowledge(batch)
    with pytest.raises(GmailConnectorError, match="not pending"):
        connector.acknowledge(batch)


def test_gmail_connector_initializes_without_replaying_existing_mail(tmp_path):
    paths = GmailPaths(tmp_path / "gmail")
    connector = GmailConnector(FakeMailbox(), paths=paths)

    batch = connector.poll()

    assert batch.initialized is True
    assert batch.messages == ()
    assert not paths.state.exists()

    connector.acknowledge(batch)

    state = json.loads(paths.state.read_text())
    assert state["history_id"] == "999"
    assert state["email_address"] == "person@example.com"
