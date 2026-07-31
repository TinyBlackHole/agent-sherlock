import os
import sqlite3
import stat
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_sherlock.application.pipeline import (
    DeliveryResult,
    MessagePipeline,
    MessageProcessingError,
    PendingDeliveryError,
    PlainMessageProcessor,
    SyncResult,
)
from agent_sherlock.connectors import ConnectorBatch
from agent_sherlock.domain import InboundMessage
from agent_sherlock.persistence import (
    MessageRepository,
    PersistenceError,
    RetentionPolicy,
)


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
        metadata={"label": "INBOX"},
    )


class FakeConnector:
    name = "gmail"

    def __init__(self, batch):
        self.batch = batch
        self.acknowledged = []

    def poll(self):
        return self.batch

    def acknowledge(self, batch):
        self.acknowledged.append(batch)


class FakeDestination:
    name = "telegram"

    def __init__(self, error=None):
        self.error = error
        self.messages = []

    def send(self, text):
        if self.error is not None:
            raise self.error
        self.messages.append(text)


def test_pipeline_persists_acknowledges_and_delivers_idempotently(tmp_path):
    message = inbound_message()
    batch = ConnectorBatch(messages=(message,), checkpoint="120")
    connector = FakeConnector(batch)
    destination = FakeDestination()
    repository = MessageRepository(tmp_path / "sherlock.db")
    pipeline = MessagePipeline(repository, destination)

    first = pipeline.sync(connector)
    second = pipeline.sync(connector)

    assert first.discovered == 1
    assert first.stored == 1
    assert first.delivered == 1
    assert second.stored == 0
    assert second.delivered == 0
    assert connector.acknowledged == [batch, batch]
    assert len(destination.messages) == 1
    assert "From: sender@example.com" in destination.messages[0]
    assert repository.pending() == ()


def test_failed_delivery_remains_pending_and_is_retried(tmp_path):
    message = inbound_message()
    repository = MessageRepository(tmp_path / "sherlock.db")
    connector = FakeConnector(ConnectorBatch(messages=(message,), checkpoint="120"))
    pipeline = MessagePipeline(
        repository,
        FakeDestination(RuntimeError("temporary failure")),
    )

    with pytest.raises(PendingDeliveryError, match="temporary failure"):
        pipeline.sync(connector)

    pending = repository.pending()
    assert len(pending) == 1
    assert pending[0].delivery_attempts == 1
    assert connector.acknowledged

    destination = FakeDestination()
    retry_pipeline = MessagePipeline(repository, destination)
    assert retry_pipeline.deliver_pending() == DeliveryResult(delivered=1)
    assert repository.pending() == ()
    assert len(destination.messages) == 1


def test_nonretryable_poison_message_is_dead_lettered_and_unblocks_queue(tmp_path):
    class NonRetryableDeliveryError(RuntimeError):
        retryable = False

    class PoisonDestination:
        name = "telegram"

        def __init__(self):
            self.calls = 0
            self.messages = []

        def send(self, text):
            self.calls += 1
            if self.calls <= 3:
                raise NonRetryableDeliveryError("invalid message")
            self.messages.append(text)

    repository = MessageRepository(tmp_path / "sherlock.db")
    destination = PoisonDestination()
    pipeline = MessagePipeline(repository, destination)

    with pytest.raises(PendingDeliveryError) as first_error:
        pipeline.ingest((inbound_message("poison"), inbound_message("healthy")))
    assert isinstance(first_error.value.cause, NonRetryableDeliveryError)
    with pytest.raises(PendingDeliveryError):
        pipeline.deliver_pending()

    result = pipeline.deliver_pending()

    assert result == DeliveryResult(delivered=1, dead_lettered=1)
    assert repository.pending() == ()
    assert len(destination.messages) == 1
    with sqlite3.connect(repository.path) as connection:
        statuses = dict(
            connection.execute(
                """
                SELECT delivery_status, COUNT(*)
                FROM inbound_messages
                GROUP BY delivery_status
                """
            )
        )
    assert statuses == {"dead_letter": 1, "delivered": 1}


def test_retryable_delivery_failure_is_never_dead_lettered(tmp_path):
    class RetryableDeliveryError(RuntimeError):
        retryable = True

    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    pipeline = MessagePipeline(
        repository,
        FakeDestination(RetryableDeliveryError("rate limited")),
    )

    for _ in range(4):
        with pytest.raises(PendingDeliveryError) as error:
            pipeline.deliver_pending()
        assert isinstance(error.value.cause, RetryableDeliveryError)

    pending = repository.pending()
    assert len(pending) == 1
    assert pending[0].delivery_attempts == 4


def test_unknown_delivery_failure_is_not_silently_dead_lettered(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    pipeline = MessagePipeline(
        repository,
        FakeDestination(RuntimeError("unexpected destination bug")),
    )

    for _ in range(4):
        with pytest.raises(PendingDeliveryError):
            pipeline.deliver_pending()

    pending = repository.pending()
    assert len(pending) == 1
    assert pending[0].delivery_attempts == 4
    assert repository.dead_letter_count() == 0


def test_processing_failure_is_fatal_without_consuming_delivery_attempt(tmp_path):
    class BrokenProcessor:
        def process(self, _message):
            raise RuntimeError("processor bug")

    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    pipeline = MessagePipeline(
        repository,
        FakeDestination(),
        processor=BrokenProcessor(),
    )

    with pytest.raises(MessageProcessingError) as error:
        pipeline.deliver_pending()

    assert isinstance(error.value.__cause__, RuntimeError)
    pending = repository.pending()
    assert len(pending) == 1
    assert pending[0].delivery_attempts == 0


def test_deliver_pending_limit_leaves_later_backlog_pending(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add(tuple(inbound_message(f"m{index}") for index in range(3)))
    destination = FakeDestination()
    pipeline = MessagePipeline(repository, destination)

    result = pipeline.deliver_pending(limit=2)

    assert result == DeliveryResult(delivered=2)
    assert len(destination.messages) == 2
    assert [message.message.external_id for message in repository.pending()] == ["m2"]


def test_pipeline_claims_each_message_immediately_before_delivery(tmp_path):
    class RecordingRepository(MessageRepository):
        def __init__(self, path):
            super().__init__(path)
            self.claim_limits = []

        def claim(self, **kwargs):
            self.claim_limits.append(kwargs["limit"])
            return super().claim(**kwargs)

    repository = RecordingRepository(tmp_path / "sherlock.db")
    repository.add(tuple(inbound_message(f"m{index}") for index in range(3)))

    result = MessagePipeline(repository, FakeDestination()).deliver_pending(limit=3)

    assert result == DeliveryResult(delivered=3)
    # A backlog-wide claim would age the later leases while earlier network
    # requests are still running.
    assert repository.claim_limits == [1, 1, 1]


def test_streaming_connector_can_ingest_normalized_messages(tmp_path):
    destination = FakeDestination()
    pipeline = MessagePipeline(
        MessageRepository(tmp_path / "sherlock.db"),
        destination,
    )

    result = pipeline.ingest((inbound_message("discord-1"),))

    assert result == SyncResult(discovered=1, stored=1, delivered=1)
    assert len(destination.messages) == 1


def test_one_pipeline_serializes_concurrent_delivery_attempts(tmp_path):
    delivery_started = threading.Event()
    release_delivery = threading.Event()

    class SlowDestination(FakeDestination):
        def send(self, text):
            delivery_started.set()
            assert release_delivery.wait(timeout=1)
            super().send(text)

    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    destination = SlowDestination()
    pipeline = MessagePipeline(repository, destination)
    results = []

    first = threading.Thread(target=lambda: results.append(pipeline.deliver_pending()))
    second = threading.Thread(target=lambda: results.append(pipeline.deliver_pending()))
    first.start()
    assert delivery_started.wait(timeout=1)
    second.start()
    release_delivery.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(result.delivered for result in results) == [0, 1]
    assert len(destination.messages) == 1


def test_pipeline_does_not_acknowledge_if_persistence_fails():
    connector = FakeConnector(
        ConnectorBatch(messages=(inbound_message(),), checkpoint="120")
    )

    class BrokenRepository:
        def add(self, _messages):
            raise PersistenceError("disk full")

    pipeline = MessagePipeline(BrokenRepository(), FakeDestination())

    with pytest.raises(PersistenceError, match="disk full"):
        pipeline.sync(connector)

    assert connector.acknowledged == []


def test_plain_processor_removes_terminal_control_characters():
    message = inbound_message()
    unsafe = InboundMessage(
        source=message.source,
        account_id=message.account_id,
        external_id=message.external_id,
        conversation_id=message.conversation_id,
        sender="\N{ESCAPE}[31mAttacker",
        subject="Hello\x00world",
        body="Body",
        received_at=message.received_at,
    )

    output = PlainMessageProcessor().process(unsafe).text

    assert "\N{ESCAPE}" not in output
    assert "\x00" not in output


def test_plain_processor_normalizes_carriage_returns_before_filtering():
    message = inbound_message()
    with_crlf = InboundMessage(
        source=message.source,
        account_id=message.account_id,
        external_id=message.external_id,
        conversation_id=message.conversation_id,
        sender=message.sender,
        subject=message.subject,
        body="first\r\nsecond\rthird",
        received_at=message.received_at,
    )

    output = PlainMessageProcessor().process(with_crlf).text

    assert output.endswith("first\nsecond\nthird")


def test_repository_creates_private_database_and_rejects_newer_schema(tmp_path):
    path = tmp_path / "private" / "sherlock.db"
    repository = MessageRepository(path)
    repository.initialize()

    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")

    with pytest.raises(PersistenceError, match="newer"):
        MessageRepository(path).initialize()


def test_repository_connections_configure_busy_timeout(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.initialize()

    connection = repository._connect()
    try:
        busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        connection.close()

    assert busy_timeout == 5_000


def test_repository_accepts_relative_database_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    repository = MessageRepository(Path("state") / "sherlock.db")
    repository.initialize()

    assert repository.path == tmp_path / "state" / "sherlock.db"
    assert repository.path.exists()


def test_repository_only_ignores_the_deduplication_constraint(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    valid = inbound_message()
    malformed = InboundMessage(
        source=valid.source,
        account_id=valid.account_id,
        external_id="malformed",
        conversation_id=valid.conversation_id,
        sender=valid.sender,
        subject=valid.subject,
        body=None,
        received_at=valid.received_at,
    )

    with pytest.raises(PersistenceError, match="persist"):
        repository.add((valid, malformed))

    # The batch transaction is all-or-nothing. A broad INSERT OR IGNORE used to
    # acknowledge the malformed event and silently lose it.
    assert repository.pending() == ()


def test_repository_counts_dead_letters_without_creating_missing_database(tmp_path):
    path = tmp_path / "sherlock.db"
    repository = MessageRepository(path)

    assert repository.dead_letter_count() == 0
    assert not path.exists()

    repository.add((inbound_message(),))
    record = repository.pending()[0]
    repository.mark_dead_letter(record.record_id, "invalid")

    assert repository.dead_letter_count() == 1


def test_repository_migrates_v1_delivery_status_for_dead_letters(tmp_path):
    path = tmp_path / "sherlock.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE inbound_messages (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                account_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (delivery_status IN ('pending', 'delivered')),
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_delivery_error TEXT NOT NULL DEFAULT '',
                delivered_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, account_id, external_id)
            );
            PRAGMA user_version = 1;
            """
        )

    repository = MessageRepository(path)
    repository.initialize()
    repository.add((inbound_message(),))
    record = repository.pending()[0]
    repository.mark_dead_letter(record.record_id, "invalid")

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        status = connection.execute(
            "SELECT delivery_status FROM inbound_messages"
        ).fetchone()[0]
    assert version == 4
    assert status == "dead_letter"


def test_two_processes_cannot_claim_the_same_message(tmp_path):
    path = tmp_path / "sherlock.db"
    MessageRepository(path).add((inbound_message(),))

    # Separate repository objects stand in for separate processes: a threading
    # lock cannot help here, so the guarantee has to come from the database.
    first = MessageRepository(path).claim(worker_id="worker-a")
    second = MessageRepository(path).claim(worker_id="worker-b")

    assert [message.message.external_id for message in first] == ["m1"]
    assert second == ()


def test_two_pipelines_deliver_each_message_once(tmp_path):
    path = tmp_path / "sherlock.db"
    MessageRepository(path).add(
        tuple(inbound_message(f"m{index}") for index in range(6))
    )
    destinations = [FakeDestination(), FakeDestination()]
    pipelines = [
        MessagePipeline(MessageRepository(path), destination)
        for destination in destinations
    ]
    results = []
    barrier = threading.Barrier(len(pipelines))

    def deliver(pipeline):
        barrier.wait(timeout=2)
        results.append(pipeline.deliver_pending())

    threads = [
        threading.Thread(target=deliver, args=(pipeline,)) for pipeline in pipelines
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert sum(result.delivered for result in results) == 6
    assert len(destinations[0].messages) + len(destinations[1].messages) == 6
    assert MessageRepository(path).pending() == ()


def test_an_expired_claim_is_reclaimed_by_another_worker(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    claimed_at = datetime(2026, 7, 30, 12, tzinfo=UTC)

    crashed = repository.claim(
        worker_id="worker-a",
        lease_seconds=60,
        now=claimed_at,
    )
    too_early = repository.claim(
        worker_id="worker-b",
        now=claimed_at + timedelta(seconds=59),
    )
    after_lease = repository.claim(
        worker_id="worker-b",
        now=claimed_at + timedelta(seconds=61),
    )

    assert len(crashed) == 1
    assert too_early == ()
    assert [message.record_id for message in after_lease] == [
        crashed[0].record_id,
    ]


def test_a_stale_worker_cannot_finish_a_reclaimed_message(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    claimed_at = datetime(2026, 7, 30, 12, tzinfo=UTC)
    stale = repository.claim(worker_id="worker-a", lease_seconds=1, now=claimed_at)
    repository.claim(worker_id="worker-b", now=claimed_at + timedelta(seconds=5))

    with pytest.raises(PersistenceError, match="claimed by another worker"):
        repository.mark_delivered(
            stale[0].record_id,
            datetime.now(UTC),
            worker_id="worker-a",
        )


def test_interrupted_delivery_releases_the_rest_of_the_batch(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add(tuple(inbound_message(f"m{index}") for index in range(3)))
    pipeline = MessagePipeline(
        repository,
        FakeDestination(RuntimeError("destination down")),
    )

    with pytest.raises(PendingDeliveryError):
        pipeline.deliver_pending()

    # Nothing may stay parked in_flight behind a lease after an aborted pass.
    assert len(repository.pending()) == 3


def test_truncated_deliveries_are_announced_and_counted(tmp_path):
    message = inbound_message()
    long_message = InboundMessage(
        source=message.source,
        account_id=message.account_id,
        external_id=message.external_id,
        conversation_id=message.conversation_id,
        sender=message.sender,
        subject=message.subject,
        body="B" * 4_000,
        received_at=message.received_at,
    )
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add((long_message,))
    destination = FakeDestination()
    pipeline = MessagePipeline(repository, destination)

    result = pipeline.deliver_pending()

    assert result == DeliveryResult(delivered=1, truncated=1)
    assert "[Truncated by Sherlock: 1001 more characters" in destination.messages[0]
    # The complete stored body stays on disk even though delivery carried less.
    with sqlite3.connect(repository.path) as connection:
        stored_body = connection.execute(
            "SELECT body FROM inbound_messages"
        ).fetchone()[0]
    assert len(stored_body) == 4_000


def test_pipeline_applies_retention_after_a_successful_delivery_pass(tmp_path):
    class RecordingRepository(MessageRepository):
        def __init__(self, path):
            super().__init__(path)
            self.retention_policies = []

        def apply_retention(self, policy=None, **_kwargs):
            self.retention_policies.append(policy)

    repository = RecordingRepository(tmp_path / "sherlock.db")
    repository.add((inbound_message(),))
    policy = RetentionPolicy(delivered_days=7)

    result = MessagePipeline(
        repository,
        FakeDestination(),
        retention=policy,
    ).deliver_pending()

    assert result == DeliveryResult(delivered=1)
    assert repository.retention_policies == [policy]


def test_delivery_body_limit_is_configurable(monkeypatch):
    monkeypatch.setenv("SHERLOCK_MAX_DELIVERY_BODY_CHARACTERS", "10000")
    assert PlainMessageProcessor().max_body_characters == 10_000

    monkeypatch.setenv("SHERLOCK_MAX_DELIVERY_BODY_CHARACTERS", "not-a-number")
    assert PlainMessageProcessor().max_body_characters == 3_000

    monkeypatch.setenv("SHERLOCK_MAX_DELIVERY_BODY_CHARACTERS", "1")
    assert PlainMessageProcessor().max_body_characters == 200


def test_retention_removes_old_delivered_messages_but_keeps_queued_work(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add(
        (inbound_message("old"), inbound_message("recent"), inbound_message("queued"))
    )
    now = datetime(2026, 7, 30, tzinfo=UTC)
    by_id = {
        stored.message.external_id: stored.record_id for stored in repository.pending()
    }
    repository.mark_delivered(by_id["old"], now - timedelta(days=45))
    repository.mark_delivered(by_id["recent"], now - timedelta(days=2))

    result = repository.apply_retention(RetentionPolicy(delivered_days=30), now=now)

    assert result.delivered_removed == 1
    counts = repository.counts()
    assert counts.delivered == 1
    assert counts.pending == 1
    assert counts.database_bytes > 0


def test_retention_trims_the_inbox_to_its_size_limit(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    repository.add(tuple(inbound_message(f"m{index}") for index in range(5)))
    now = datetime(2026, 7, 30, tzinfo=UTC)
    for stored in repository.pending()[:4]:
        repository.mark_delivered(stored.record_id, now)

    result = repository.apply_retention(
        RetentionPolicy(delivered_days=365, dead_letter_days=365, max_messages=3),
        now=now,
    )

    assert result.over_limit_removed == 2
    counts = repository.counts()
    assert counts.total == 3
    # Undelivered work is never sacrificed to make room.
    assert counts.pending == 1


def test_dead_letter_retention_starts_when_delivery_gives_up(tmp_path):
    repository = MessageRepository(tmp_path / "sherlock.db")
    message = inbound_message()
    old_message = InboundMessage(
        source=message.source,
        account_id=message.account_id,
        external_id=message.external_id,
        conversation_id=message.conversation_id,
        sender=message.sender,
        subject=message.subject,
        body=message.body,
        received_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    repository.add((old_message,))
    repository.mark_dead_letter(repository.pending()[0].record_id, "invalid")

    result = repository.apply_retention(
        RetentionPolicy(dead_letter_days=30),
        now=datetime.now(UTC),
    )

    assert result.dead_letters_removed == 0
    assert repository.counts().dead_letter == 1


def test_repository_migrates_a_v2_database_and_keeps_queued_work(tmp_path):
    path = tmp_path / "sherlock.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE inbound_messages (
                id INTEGER PRIMARY KEY,
                source TEXT NOT NULL,
                account_id TEXT NOT NULL,
                external_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                sender TEXT NOT NULL,
                subject TEXT NOT NULL,
                body TEXT NOT NULL,
                received_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (delivery_status IN
                        ('pending', 'delivered', 'dead_letter')),
                delivery_attempts INTEGER NOT NULL DEFAULT 0,
                last_delivery_error TEXT NOT NULL DEFAULT '',
                delivered_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source, account_id, external_id)
            );
            INSERT INTO inbound_messages (
                source, account_id, external_id, conversation_id, sender,
                subject, body, received_at, metadata_json, delivery_status,
                delivery_attempts
            ) VALUES (
                'gmail', 'person@example.com', 'queued', 'thread-1',
                'sender@example.com', 'Hello', 'Body', '2026-07-30T00:00:00+00:00',
                '{}', 'pending', 2
            );
            PRAGMA user_version = 2;
            """
        )

    repository = MessageRepository(path)
    claimed = repository.claim(worker_id="worker-a")

    with sqlite3.connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 4
    assert [message.message.external_id for message in claimed] == ["queued"]
    assert claimed[0].delivery_attempts == 2
