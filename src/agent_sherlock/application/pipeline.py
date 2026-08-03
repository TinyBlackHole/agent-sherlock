from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agent_sherlock.connectors import PollingConnector
from agent_sherlock.destinations import MessageDestination
from agent_sherlock.domain import InboundMessage
from agent_sherlock.persistence import (
    MessageRepository,
    RetentionPolicy,
    StoredMessage,
)

MAX_DELIVERY_ATTEMPTS = 3
MAX_PROCESSING_ATTEMPTS = 3
MAX_DELIVERY_HEADER_CHARACTERS = 500
# How much of a stored message body a single delivery carries. This processor
# does not alter persistence; whatever does not fit is announced inside the
# delivered text and counted in the operational result.
DEFAULT_MAX_DELIVERY_BODY_CHARACTERS = 3_000
MAX_DELIVERY_BODY_CHARACTERS_ENV = "SHERLOCK_MAX_DELIVERY_BODY_CHARACTERS"
MIN_DELIVERY_BODY_CHARACTERS = 200
MAX_DELIVERY_BODY_CHARACTERS_CEILING = 100_000
# Retention is enforced from the delivery path, but running a DELETE for every
# event would be wasted work, so it runs at most once per interval per process.
RETENTION_INTERVAL_SECONDS = 3_600.0


class PipelineError(RuntimeError):
    """Base class for expected message-pipeline failures."""


class MessageIngestError(PipelineError):
    """Raised when a streaming event could not be persisted.

    Gateways and webhooks do not replay events, so the caller must stop and make
    the loss visible instead of continuing silently. Delivery failures raise
    `PendingDeliveryError` instead, because by then the message is durable.
    """

    def __init__(self, cause: Exception):
        super().__init__(f"{cause} The event was not stored.")
        self.cause = cause


class MessageProcessingError(PipelineError):
    """Raised when a queued message cannot be transformed safely."""


class PendingProcessingError(MessageProcessingError):
    """Raised when local processing failed but the message remains queued."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause

    @property
    def retry_after(self) -> object | None:
        return getattr(self.cause, "retry_after", None)


class PendingDeliveryError(PipelineError):
    """Raised when destination delivery failed but the message remains queued."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause

    @property
    def retry_after(self) -> object | None:
        return getattr(self.cause, "retry_after", None)


@dataclass(frozen=True, slots=True)
class ProcessedMessage:
    """Delivery text plus what the processor had to leave out of it."""

    text: str
    omitted_characters: int = 0
    processor_name: str = "plain"
    processor_model: str = ""
    processor_model_digest: str = ""
    discord_notification_user_id: str = ""


class MessageProcessor(Protocol):
    """Transforms an untrusted inbound message into safe output text."""

    def process(self, message: InboundMessage) -> ProcessedMessage | str: ...


@dataclass(frozen=True, slots=True)
class _TrimmedText:
    text: str
    omitted_characters: int = 0


class PlainMessageProcessor:
    """Deterministic processor used while local AI is disabled."""

    def __init__(self, max_body_characters: int | None = None):
        self.max_body_characters = (
            max_delivery_body_characters()
            if max_body_characters is None
            else max_body_characters
        )

    def process(self, message: InboundMessage) -> ProcessedMessage:
        sender = _safe_delivery_text(
            message.sender,
            fallback="(unknown sender)",
            max_characters=MAX_DELIVERY_HEADER_CHARACTERS,
        )
        subject = _safe_delivery_text(
            message.subject,
            fallback="(no subject)",
            max_characters=MAX_DELIVERY_HEADER_CHARACTERS,
        )
        body = _safe_delivery_text(
            message.body,
            fallback="(empty message)",
            max_characters=self.max_body_characters,
        )
        source = _safe_delivery_text(
            message.source.upper(),
            max_characters=MAX_DELIVERY_HEADER_CHARACTERS,
        )
        text = (
            f"New {source.text} message\nFrom: {sender.text}\n"
            f"Subject: {subject.text}\n\n{body.text}"
        )
        omitted = body.omitted_characters
        if omitted:
            text = (
                f"{text}\n\n[Truncated by Sherlock: {omitted} more characters were "
                f"not delivered. Set {MAX_DELIVERY_BODY_CHARACTERS_ENV} to raise the "
                f"{self.max_body_characters}-character limit.]"
            )
        return ProcessedMessage(text=text, omitted_characters=omitted)


@dataclass(frozen=True, slots=True)
class SyncResult:
    discovered: int = 0
    stored: int = 0
    delivered: int = 0
    dead_lettered: int = 0
    truncated: int = 0
    initialized: bool = False
    history_reset: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivered: int = 0
    dead_lettered: int = 0
    truncated: int = 0


class MessagePipeline:
    """Persist connector messages, checkpoint them, then deliver pending work."""

    def __init__(
        self,
        repository: MessageRepository,
        destination: MessageDestination,
        processor: MessageProcessor | None = None,
        *,
        retention: RetentionPolicy | None = None,
        worker_id: str | None = None,
    ):
        self.repository = repository
        self.destination = destination
        self.processor = processor or PlainMessageProcessor()
        self.retention = retention or RetentionPolicy()
        # Claims are recorded under this identifier, so a second Sherlock process
        # can tell the difference between its own rows and someone else's.
        self.worker_id = worker_id or _default_worker_id()
        self._delivery_lock = threading.Lock()
        self._next_retention_at = 0.0

    def sync(self, connector: PollingConnector) -> SyncResult:
        batch = connector.poll()
        # Polling connectors replay from their checkpoint, so a storage failure
        # here is recoverable and is deliberately raised unwrapped.
        stored = self.repository.add(batch.messages)

        # A provider checkpoint is acknowledged only after every normalized event
        # is durable. Replaying after a later crash is harmless because inserts are
        # idempotent.
        connector.acknowledge(batch)
        delivery = self.deliver_pending()
        return SyncResult(
            discovered=len(batch.messages),
            stored=stored,
            delivered=delivery.delivered,
            dead_lettered=delivery.dead_lettered,
            truncated=delivery.truncated,
            initialized=batch.initialized,
            history_reset=batch.history_reset,
        )

    def store(self, messages: tuple[InboundMessage, ...]) -> int:
        """Persist streaming events without delivering them.

        Callers that cannot replay their input use this to separate a storage
        failure (the event is gone) from a delivery failure (the event is safe
        on disk and will be retried).
        """
        try:
            return self.repository.add(messages)
        except Exception as exc:
            raise MessageIngestError(exc) from exc

    def ingest(self, messages: tuple[InboundMessage, ...]) -> SyncResult:
        """Persist and deliver events from streaming or webhook connectors."""
        stored = self.store(messages)
        delivery = self.deliver_pending()
        return SyncResult(
            discovered=len(messages),
            stored=stored,
            delivered=delivery.delivered,
            dead_lettered=delivery.dead_lettered,
            truncated=delivery.truncated,
        )

    def deliver_pending(self, *, limit: int = 100) -> DeliveryResult:
        with self._delivery_lock:
            result = self._deliver_pending(limit=limit)
        self._apply_retention()
        return result

    def _deliver_pending(self, *, limit: int) -> DeliveryResult:
        delivered = 0
        dead_lettered = 0
        truncated = 0
        # Claim immediately before each send. Claiming a whole backlog at once
        # starts every lease before the first network request; a slow destination
        # could then let later leases expire before this worker reaches them.
        for _ in range(max(limit, 0)):
            claimed = self.repository.claim(worker_id=self.worker_id, limit=1)
            if not claimed:
                break
            try:
                stored_message = claimed[0]
                outcome = self._deliver_one(stored_message)
                delivered += outcome.delivered
                dead_lettered += outcome.dead_lettered
                truncated += outcome.truncated
            except BaseException:
                # Processing failures and interrupts happen before a delivery
                # state transition. Release this one row instead of parking it
                # behind the lease.
                self._release(claimed)
                raise
        return DeliveryResult(
            delivered=delivered,
            dead_lettered=dead_lettered,
            truncated=truncated,
        )

    def _deliver_one(self, stored_message: StoredMessage) -> DeliveryResult:
        if stored_message.processed_text is None:
            try:
                processed = _as_processed(
                    self.processor.process(stored_message.message)
                )
            except Exception as exc:
                retryable = getattr(exc, "retryable", None)
                if type(retryable) is bool:
                    attempt = stored_message.processing_attempts + 1
                    if _should_dead_letter(
                        exc,
                        attempt=attempt,
                        maximum_attempts=MAX_PROCESSING_ATTEMPTS,
                    ):
                        self.repository.mark_processing_dead_letter(
                            stored_message.record_id,
                            str(exc),
                            worker_id=self.worker_id,
                        )
                        return DeliveryResult(dead_lettered=1)
                    self.repository.mark_processing_failed(
                        stored_message.record_id,
                        str(exc),
                        worker_id=self.worker_id,
                    )
                    raise PendingProcessingError(exc) from exc
                raise MessageProcessingError(
                    f"Cannot process a queued message safely: {exc}"
                ) from exc
            self.repository.save_processed(
                stored_message.record_id,
                text=processed.text,
                omitted_characters=processed.omitted_characters,
                processor_name=processed.processor_name,
                processor_model=processed.processor_model,
                processor_model_digest=processed.processor_model_digest,
                discord_notification_user_id=(processed.discord_notification_user_id),
                processed_at=datetime.now(UTC),
                worker_id=self.worker_id,
            )
        else:
            processed = ProcessedMessage(
                text=stored_message.processed_text,
                omitted_characters=stored_message.processed_omitted_characters,
                processor_name=stored_message.processor_name,
                processor_model=stored_message.processor_model,
                processor_model_digest=stored_message.processor_model_digest,
                discord_notification_user_id=(
                    stored_message.discord_notification_user_id
                ),
            )

        try:
            if processed.discord_notification_user_id:
                self.destination.send_important(
                    processed.text,
                    discord_user_id=processed.discord_notification_user_id,
                )
            else:
                self.destination.send(processed.text)
        except Exception as exc:
            attempt = stored_message.delivery_attempts + 1
            if _should_dead_letter(
                exc,
                attempt=attempt,
                maximum_attempts=MAX_DELIVERY_ATTEMPTS,
            ):
                self.repository.mark_dead_letter(
                    stored_message.record_id,
                    str(exc),
                    worker_id=self.worker_id,
                )
                return DeliveryResult(dead_lettered=1)
            self.repository.mark_failed(
                stored_message.record_id,
                str(exc),
                worker_id=self.worker_id,
            )
            raise PendingDeliveryError(exc) from exc
        self.repository.mark_delivered(
            stored_message.record_id,
            datetime.now(UTC),
            worker_id=self.worker_id,
        )
        return DeliveryResult(
            delivered=1,
            truncated=1 if processed.omitted_characters else 0,
        )

    def _release(self, messages: Iterable[StoredMessage]) -> None:
        record_ids = tuple(message.record_id for message in messages)
        if not record_ids:
            return
        try:
            self.repository.release(record_ids, worker_id=self.worker_id)
        except Exception:
            # The lease expires on its own; failing to release must never mask
            # the error that interrupted delivery.
            return

    def _apply_retention(self) -> None:
        now = time.monotonic()
        if now < self._next_retention_at:
            return
        self._next_retention_at = now + RETENTION_INTERVAL_SECONDS
        try:
            self.repository.apply_retention(self.retention)
        except Exception:
            # Retention is housekeeping: a successful delivery pass must not fail
            # because old rows could not be trimmed. Retry soon instead of
            # suppressing housekeeping for the full normal interval.
            self._next_retention_at = now + min(60.0, RETENTION_INTERVAL_SECONDS)


def max_delivery_body_characters() -> int:
    """Read the configurable per-delivery body budget."""
    raw = os.environ.get(MAX_DELIVERY_BODY_CHARACTERS_ENV, "").strip()
    if not raw:
        return DEFAULT_MAX_DELIVERY_BODY_CHARACTERS
    try:
        configured = int(raw)
    except ValueError:
        return DEFAULT_MAX_DELIVERY_BODY_CHARACTERS
    return max(
        MIN_DELIVERY_BODY_CHARACTERS,
        min(configured, MAX_DELIVERY_BODY_CHARACTERS_CEILING),
    )


def _as_processed(result: ProcessedMessage | str) -> ProcessedMessage:
    if isinstance(result, ProcessedMessage):
        return result
    if isinstance(result, str):
        return ProcessedMessage(text=result)
    raise TypeError("A message processor must return text.")


def _default_worker_id() -> str:
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown-host"
    return f"{host[:64]}:{os.getpid()}:{secrets.token_hex(4)}"


def _should_dead_letter(
    exception: Exception,
    *,
    attempt: int,
    maximum_attempts: int,
) -> bool:
    return (
        getattr(exception, "retryable", None) is False and attempt >= maximum_attempts
    )


def _safe_delivery_text(
    value: str,
    *,
    fallback: str = "",
    max_characters: int = DEFAULT_MAX_DELIVERY_BODY_CHARACTERS,
) -> _TrimmedText:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    safe_characters: list[str] = []
    for character in normalized:
        if character in {"\n", "\t"} or character.isprintable():
            safe_characters.append(character)
        else:
            safe_characters.append(" ")
    cleaned = "".join(safe_characters).strip()
    omitted = 0
    if len(cleaned) > max_characters:
        omitted = len(cleaned) - (max_characters - 1)
        cleaned = f"{cleaned[: max_characters - 1]}…"
    return _TrimmedText(text=cleaned or fallback, omitted_characters=omitted)
