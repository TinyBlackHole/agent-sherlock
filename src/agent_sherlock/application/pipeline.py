from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from agent_sherlock.connectors import PollingConnector
from agent_sherlock.destinations import MessageDestination
from agent_sherlock.domain import InboundMessage
from agent_sherlock.persistence import MessageRepository

MAX_DELIVERY_ATTEMPTS = 3
MAX_DELIVERY_BODY_CHARACTERS = 3_000
MAX_DELIVERY_HEADER_CHARACTERS = 500


class PipelineError(RuntimeError):
    """Base class for expected message-pipeline failures."""


class MessageProcessingError(PipelineError):
    """Raised when a queued message cannot be transformed safely."""


class PendingDeliveryError(PipelineError):
    """Raised when destination delivery failed but the message remains queued."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause

    @property
    def retry_after(self) -> object | None:
        return getattr(self.cause, "retry_after", None)


class MessageProcessor(Protocol):
    """Transforms an untrusted inbound message into safe output text."""

    def process(self, message: InboundMessage) -> str: ...


class PlainMessageProcessor:
    """Temporary deterministic processor until an AI provider is configured."""

    def process(self, message: InboundMessage) -> str:
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
            max_characters=MAX_DELIVERY_BODY_CHARACTERS,
        )
        source = _safe_delivery_text(
            message.source.upper(),
            max_characters=MAX_DELIVERY_HEADER_CHARACTERS,
        )
        return f"New {source} message\nFrom: {sender}\nSubject: {subject}\n\n{body}"


@dataclass(frozen=True, slots=True)
class SyncResult:
    discovered: int = 0
    stored: int = 0
    delivered: int = 0
    dead_lettered: int = 0
    initialized: bool = False
    history_reset: bool = False


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    delivered: int = 0
    dead_lettered: int = 0


class MessagePipeline:
    """Persist connector messages, checkpoint them, then deliver pending work."""

    def __init__(
        self,
        repository: MessageRepository,
        destination: MessageDestination,
        processor: MessageProcessor | None = None,
    ):
        self.repository = repository
        self.destination = destination
        self.processor = processor or PlainMessageProcessor()
        self._delivery_lock = threading.Lock()

    def sync(self, connector: PollingConnector) -> SyncResult:
        batch = connector.poll()
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
            initialized=batch.initialized,
            history_reset=batch.history_reset,
        )

    def ingest(self, messages: tuple[InboundMessage, ...]) -> SyncResult:
        """Persist and deliver events from streaming or webhook connectors."""
        stored = self.repository.add(messages)
        delivery = self.deliver_pending()
        return SyncResult(
            discovered=len(messages),
            stored=stored,
            delivered=delivery.delivered,
            dead_lettered=delivery.dead_lettered,
        )

    def deliver_pending(self, *, limit: int = 100) -> DeliveryResult:
        with self._delivery_lock:
            return self._deliver_pending(limit=limit)

    def _deliver_pending(self, *, limit: int) -> DeliveryResult:
        delivered = 0
        dead_lettered = 0
        for stored_message in self.repository.pending(limit=limit):
            try:
                text = self.processor.process(stored_message.message)
            except Exception as exc:
                raise MessageProcessingError(
                    "Cannot process a queued message safely."
                ) from exc

            try:
                self.destination.send(text)
            except Exception as exc:
                attempt = stored_message.delivery_attempts + 1
                if _should_dead_letter(exc, attempt=attempt):
                    self.repository.mark_dead_letter(
                        stored_message.record_id,
                        str(exc),
                    )
                    dead_lettered += 1
                    continue
                self.repository.mark_failed(stored_message.record_id, str(exc))
                raise PendingDeliveryError(exc) from exc
            self.repository.mark_delivered(
                stored_message.record_id,
                datetime.now(UTC),
            )
            delivered += 1
        return DeliveryResult(
            delivered=delivered,
            dead_lettered=dead_lettered,
        )


def _should_dead_letter(exception: Exception, *, attempt: int) -> bool:
    return (
        getattr(exception, "retryable", None) is False
        and attempt >= MAX_DELIVERY_ATTEMPTS
    )


def _safe_delivery_text(
    value: str,
    *,
    fallback: str = "",
    max_characters: int = MAX_DELIVERY_BODY_CHARACTERS,
) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    safe_characters: list[str] = []
    for character in normalized:
        if character in {"\n", "\t"} or character.isprintable():
            safe_characters.append(character)
        else:
            safe_characters.append(" ")
    cleaned = "".join(safe_characters).strip()
    if len(cleaned) > max_characters:
        cleaned = f"{cleaned[: max_characters - 1]}…"
    return cleaned or fallback
