"""Durable local persistence."""

from agent_sherlock.persistence.sqlite import (
    DEFAULT_DEAD_LETTER_RETENTION_DAYS,
    DEFAULT_DELIVERED_RETENTION_DAYS,
    DEFAULT_MAX_STORED_MESSAGES,
    MessageCounts,
    MessageRepository,
    PersistenceError,
    RetentionPolicy,
    RetentionResult,
    StoredMessage,
)

__all__ = [
    "DEFAULT_DEAD_LETTER_RETENTION_DAYS",
    "DEFAULT_DELIVERED_RETENTION_DAYS",
    "DEFAULT_MAX_STORED_MESSAGES",
    "MessageCounts",
    "MessageRepository",
    "PersistenceError",
    "RetentionPolicy",
    "RetentionResult",
    "StoredMessage",
]
