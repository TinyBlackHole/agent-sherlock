from datetime import UTC, datetime, timedelta

from agent_sherlock.cli import main
from agent_sherlock.domain import InboundMessage
from agent_sherlock.persistence import MessageRepository


def inbound_message(external_id="m1"):
    return InboundMessage(
        source="gmail",
        account_id="person@example.com",
        external_id=external_id,
        conversation_id="thread-1",
        sender="sender@example.com",
        subject="Hello",
        body="Message body",
        received_at=datetime(2026, 7, 30, tzinfo=UTC),
        metadata={},
    )


def test_purge_reports_an_empty_inbox(capsys):
    assert main(["purge"]) == 0

    output = capsys.readouterr().out
    assert "Nothing to purge" in output
    assert "0 stored messages" in output


def test_purge_removes_old_delivered_messages_and_keeps_queued_work(capsys):
    repository = MessageRepository()
    repository.add((inbound_message("old"), inbound_message("queued")))
    old = next(
        stored for stored in repository.pending() if stored.message.external_id == "old"
    )
    repository.mark_delivered(
        old.record_id,
        datetime.now(UTC) - timedelta(days=90),
    )

    assert main(["purge"]) == 0

    output = capsys.readouterr().out
    assert "Purged 1 stored message" in output
    assert "1 pending" in output
    assert repository.counts().total == 1


def test_purge_all_clears_every_handled_message(capsys):
    repository = MessageRepository()
    repository.add(
        (
            inbound_message("delivered"),
            inbound_message("dead"),
        )
    )
    stored = {
        message.message.external_id: message.record_id
        for message in repository.pending()
    }
    repository.mark_delivered(stored["delivered"], datetime.now(UTC))
    repository.mark_dead_letter(stored["dead"], "invalid")

    assert main(["purge", "--all"]) == 0

    counts = repository.counts()
    assert counts.total == 0
    assert "Purged 2 stored messages" in capsys.readouterr().out


def test_purge_status_reports_without_deleting(capsys):
    repository = MessageRepository()
    repository.add((inbound_message(),))
    repository.mark_delivered(
        repository.pending()[0].record_id,
        datetime.now(UTC) - timedelta(days=365),
    )

    assert main(["purge", "--status"]) == 0

    assert "1 delivered" in capsys.readouterr().out
    assert repository.counts().total == 1


def test_purge_rejects_a_negative_retention_window(capsys):
    assert main(["purge", "--days", "-1"]) == 2
    assert "cannot be negative" in capsys.readouterr().err
