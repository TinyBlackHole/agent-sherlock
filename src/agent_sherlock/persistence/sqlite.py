from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from agent_sherlock.domain import InboundMessage
from agent_sherlock.storage import (
    PRIVATE_FILE_MODE,
    StorageError,
    config_root,
    ensure_private_directory,
    harden_private_file,
)

SCHEMA_VERSION = 4
DATABASE_BUSY_TIMEOUT_MS = 5_000
MAX_STORED_ERROR_CHARACTERS = 1_000
MAX_PROCESSED_TEXT_CHARACTERS = 250_000
MAX_PROCESSOR_METADATA_CHARACTERS = 1_000
# A claim is a lease, not a lock: a worker that crashes mid-delivery must not
# strand its rows, so another worker reclaims them once the lease expires.
DEFAULT_CLAIM_LEASE_SECONDS = 300.0
DEFAULT_DELIVERED_RETENTION_DAYS = 30
DEFAULT_DEAD_LETTER_RETENTION_DAYS = 90
DEFAULT_MAX_STORED_MESSAGES = 50_000

_MESSAGE_COLUMNS = """
    id,
    source,
    account_id,
    external_id,
    conversation_id,
    sender,
    subject,
    body,
    received_at,
    metadata_json,
    delivery_attempts,
    processed_text,
    processed_omitted_characters,
    processor_name,
    processor_config_hash,
    processor_model,
    processor_model_digest
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS inbound_messages (
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
            ('pending', 'in_flight', 'delivered', 'dead_letter')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_delivery_error TEXT NOT NULL DEFAULT '',
    claimed_by TEXT NOT NULL DEFAULT '',
    claim_expires_at TEXT,
    processed_text TEXT,
    processed_omitted_characters INTEGER NOT NULL DEFAULT 0
        CHECK (processed_omitted_characters >= 0),
    processor_name TEXT NOT NULL DEFAULT '',
    processor_config_hash TEXT NOT NULL DEFAULT '',
    processor_model TEXT NOT NULL DEFAULT '',
    processor_model_digest TEXT NOT NULL DEFAULT '',
    processed_at TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, account_id, external_id)
);
"""

_CREATE_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_pending
ON inbound_messages (delivery_status, received_at, id);
"""

_CREATE_RETENTION_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_retention
ON inbound_messages (delivery_status, delivered_at, id);
"""

# Columns shared by every schema version Sherlock has ever written. Newer
# columns are left at their defaults when an old database is migrated.
_LEGACY_COLUMNS = (
    "id",
    "source",
    "account_id",
    "external_id",
    "conversation_id",
    "sender",
    "subject",
    "body",
    "received_at",
    "metadata_json",
    "delivery_status",
    "delivery_attempts",
    "last_delivery_error",
    "delivered_at",
    "created_at",
)


def _with_message_columns(statement: str) -> str:
    """Insert the fixed projection used to deserialize stored messages."""
    return statement.replace("{message_columns}", _MESSAGE_COLUMNS)


def _with_legacy_columns(statement: str) -> str:
    """Insert only the fixed, versioned legacy column allowlist."""
    columns = ",\n                ".join(_LEGACY_COLUMNS)
    return statement.replace("{legacy_columns}", columns)


def _with_record_id_placeholders(statement: str, count: int) -> str:
    """Create a parameterized IN clause without interpolating record values."""
    placeholders = ",".join("?" for _ in range(count))
    return statement.replace("{record_ids}", placeholders)


class PersistenceError(RuntimeError):
    """Raised when the durable inbox cannot be read or updated safely."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    record_id: int
    message: InboundMessage
    delivery_attempts: int
    processed_text: str | None = None
    processed_omitted_characters: int = 0
    processor_name: str = ""
    processor_config_hash: str = ""
    processor_model: str = ""
    processor_model_digest: str = ""


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """How long delivered and undeliverable messages stay on disk."""

    delivered_days: int = DEFAULT_DELIVERED_RETENTION_DAYS
    dead_letter_days: int = DEFAULT_DEAD_LETTER_RETENTION_DAYS
    max_messages: int = DEFAULT_MAX_STORED_MESSAGES


@dataclass(frozen=True, slots=True)
class RetentionResult:
    delivered_removed: int = 0
    dead_letters_removed: int = 0
    over_limit_removed: int = 0

    @property
    def total(self) -> int:
        return (
            self.delivered_removed + self.dead_letters_removed + self.over_limit_removed
        )


@dataclass(frozen=True, slots=True)
class MessageCounts:
    pending: int = 0
    in_flight: int = 0
    delivered: int = 0
    dead_letter: int = 0
    database_bytes: int = 0

    @property
    def total(self) -> int:
        return self.pending + self.in_flight + self.delivered + self.dead_letter


class MessageRepository:
    """SQLite inbox with idempotent inserts and durable delivery state."""

    def __init__(self, path: Path | None = None):
        selected_path = path or config_root() / "sherlock.db"
        self.path = selected_path.expanduser().absolute()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        try:
            ensure_private_directory(
                self.path.parent,
                preserve_existing_mode=self.path.parent == config_root(),
            )
            self._prepare_database_file()
            with closing(self._connect()) as connection:
                current_version = connection.execute("PRAGMA user_version").fetchone()[
                    0
                ]
                if current_version > SCHEMA_VERSION:
                    raise PersistenceError(
                        "The message database was created by a newer Sherlock version."
                    )
                has_messages_table = connection.execute(
                    """
                    SELECT 1
                    FROM sqlite_master
                    WHERE type = 'table' AND name = 'inbound_messages'
                    """
                ).fetchone()
                if current_version in {1, 2, 3} or (
                    current_version == 0 and has_messages_table is not None
                ):
                    self._migrate_legacy(connection)
                else:
                    self._create_current_schema(connection)
            if os.name == "posix":
                self.path.chmod(PRIVATE_FILE_MODE)
            self._initialized = True
        except (OSError, sqlite3.Error, StorageError) as exc:
            raise PersistenceError(
                f"Cannot initialize Sherlock's message database: {self.path}"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=DATABASE_BUSY_TIMEOUT_MS / 1_000,
            isolation_level=None,
        )
        try:
            connection.execute(f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
        except sqlite3.Error:
            connection.close()
            raise
        return connection

    @staticmethod
    def _create_current_schema(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_CREATE_MESSAGES_TABLE)
            connection.execute(_CREATE_PENDING_INDEX)
            connection.execute(_CREATE_RETENTION_INDEX)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            connection.rollback()
            raise
        connection.commit()

    @staticmethod
    def _migrate_legacy(connection: sqlite3.Connection) -> None:
        """Rebuild any older schema into the current one, preserving every row."""
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP INDEX IF EXISTS idx_inbound_pending")
            connection.execute("DROP INDEX IF EXISTS idx_inbound_retention")
            connection.execute(
                "ALTER TABLE inbound_messages RENAME TO inbound_messages_legacy"
            )
            connection.execute(_CREATE_MESSAGES_TABLE)
            connection.execute(_CREATE_PENDING_INDEX)
            connection.execute(_CREATE_RETENTION_INDEX)
            connection.execute(
                _with_legacy_columns(
                    """
                INSERT INTO inbound_messages (
                    {legacy_columns}
                )
                SELECT
                    {legacy_columns}
                FROM inbound_messages_legacy;
                """
                )
            )
            connection.execute("DROP TABLE inbound_messages_legacy")
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            connection.rollback()
            raise
        connection.commit()

    def _prepare_database_file(self) -> None:
        if not self.path.exists():
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
            if hasattr(os, "O_CLOEXEC"):
                flags |= os.O_CLOEXEC
            try:
                descriptor = os.open(self.path, flags, PRIVATE_FILE_MODE)
            except FileExistsError:
                pass
            else:
                os.close(descriptor)
        harden_private_file(self.path)

    def add(self, messages: tuple[InboundMessage, ...]) -> int:
        if not messages:
            return 0
        self.initialize()
        inserted = 0
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    for message in messages:
                        metadata = json.dumps(
                            message.metadata,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                        cursor = connection.execute(
                            """
                            INSERT INTO inbound_messages (
                                source,
                                account_id,
                                external_id,
                                conversation_id,
                                sender,
                                subject,
                                body,
                                received_at,
                                metadata_json
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT (source, account_id, external_id)
                            DO NOTHING
                            """,
                            (
                                message.source,
                                message.account_id,
                                message.external_id,
                                message.conversation_id,
                                message.sender,
                                message.subject,
                                message.body,
                                message.received_at.isoformat(),
                                metadata,
                            ),
                        )
                        inserted += cursor.rowcount
                except Exception:
                    connection.rollback()
                    raise
                connection.commit()
        except (TypeError, ValueError, sqlite3.Error) as exc:
            raise PersistenceError("Cannot persist incoming messages.") from exc
        return inserted

    def pending(self, *, limit: int = 100) -> tuple[StoredMessage, ...]:
        """Read queued work without claiming it. Use `claim` before delivering."""
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    _with_message_columns(
                        """
                    SELECT {message_columns}
                    FROM inbound_messages
                    WHERE delivery_status = 'pending'
                    ORDER BY received_at, id
                    LIMIT ?
                    """
                    ),
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot load pending messages.") from exc
        return self._stored_messages(rows)

    def claim(
        self,
        *,
        worker_id: str,
        limit: int = 100,
        lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
        now: datetime | None = None,
    ) -> tuple[StoredMessage, ...]:
        """Atomically take ownership of queued work.

        Selection and marking happen inside one `BEGIN IMMEDIATE` transaction, so
        two concurrent Sherlock processes can never hand the same row to their
        destinations. Rows whose lease has expired are reclaimed, which is what
        makes a crashed worker's backlog recoverable.
        """
        if not worker_id:
            raise PersistenceError("A delivery worker needs a non-empty identifier.")
        self.initialize()
        claimed_at = now or datetime.now(UTC)
        expires_at = claimed_at + timedelta(seconds=max(lease_seconds, 1.0))
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    rows = connection.execute(
                        _with_message_columns(
                            """
                        SELECT {message_columns}
                        FROM inbound_messages
                        WHERE delivery_status = 'pending'
                           OR (
                                delivery_status = 'in_flight'
                                AND claim_expires_at IS NOT NULL
                                AND claim_expires_at <= ?
                           )
                        ORDER BY received_at, id
                        LIMIT ?
                        """
                        ),
                        (claimed_at.isoformat(), limit),
                    ).fetchall()
                    record_ids = [row[0] for row in rows]
                    if record_ids:
                        connection.execute(
                            _with_record_id_placeholders(
                                """
                            UPDATE inbound_messages
                            SET delivery_status = 'in_flight',
                                claimed_by = ?,
                                claim_expires_at = ?
                            WHERE id IN ({record_ids})
                            """,
                                len(record_ids),
                            ),
                            (worker_id, expires_at.isoformat(), *record_ids),
                        )
                except Exception:
                    connection.rollback()
                    raise
                connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot claim pending messages.") from exc
        return self._stored_messages(rows)

    def release(self, record_ids: tuple[int, ...], *, worker_id: str) -> int:
        """Return claimed-but-unattempted rows to the queue immediately."""
        if not record_ids:
            return 0
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(
                    _with_record_id_placeholders(
                        """
                    UPDATE inbound_messages
                    SET delivery_status = 'pending',
                        claimed_by = '',
                        claim_expires_at = NULL
                    WHERE id IN ({record_ids})
                      AND delivery_status = 'in_flight'
                      AND claimed_by = ?
                    """,
                        len(record_ids),
                    ),
                    (*record_ids, worker_id),
                )
                connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot release claimed messages.") from exc
        return cursor.rowcount

    def dead_letter_count(self) -> int:
        if not self.path.exists() and not self.path.is_symlink():
            return 0
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    """
                    SELECT COUNT(*)
                    FROM inbound_messages
                    WHERE delivery_status = 'dead_letter'
                    """
                ).fetchone()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot count dead-letter messages.") from exc
        if row is None or type(row[0]) is not int:
            raise PersistenceError("The message database returned an invalid count.")
        return row[0]

    def counts(self) -> MessageCounts:
        """Report inbox size so operators can see the database growing."""
        if not self.path.exists() and not self.path.is_symlink():
            return MessageCounts()
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT delivery_status, COUNT(*)
                    FROM inbound_messages
                    GROUP BY delivery_status
                    """
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot summarize the message database.") from exc
        totals = {str(row[0]): int(row[1]) for row in rows}
        try:
            database_bytes = self.path.stat().st_size
        except OSError:
            database_bytes = 0
        return MessageCounts(
            pending=totals.get("pending", 0),
            in_flight=totals.get("in_flight", 0),
            delivered=totals.get("delivered", 0),
            dead_letter=totals.get("dead_letter", 0),
            database_bytes=database_bytes,
        )

    def mark_delivered(
        self,
        record_id: int,
        delivered_at: datetime,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_status = 'delivered',
                delivery_attempts = delivery_attempts + 1,
                last_delivery_error = '',
                claimed_by = '',
                claim_expires_at = NULL,
                delivered_at = ?
            WHERE id = ?
            """,
            (delivered_at.isoformat(), record_id),
            worker_id=worker_id,
        )

    def save_processed(
        self,
        record_id: int,
        *,
        text: str,
        omitted_characters: int,
        processor_name: str,
        processor_config_hash: str,
        processor_model: str,
        processor_model_digest: str,
        processed_at: datetime,
        worker_id: str,
    ) -> None:
        """Persist deterministic delivery text while this worker owns the row."""
        if not isinstance(text, str) or not text:
            raise PersistenceError("Processed message text must not be empty.")
        if len(text) > MAX_PROCESSED_TEXT_CHARACTERS:
            raise PersistenceError("Processed message text exceeds its safety limit.")
        if type(omitted_characters) is not int or omitted_characters < 0:
            raise PersistenceError("Processed omitted-character count is invalid.")
        metadata = (
            processor_name,
            processor_config_hash,
            processor_model,
            processor_model_digest,
        )
        if any(
            not isinstance(value, str) or len(value) > MAX_PROCESSOR_METADATA_CHARACTERS
            for value in metadata
        ):
            raise PersistenceError("Processed message metadata is invalid.")
        if processed_at.tzinfo is None:
            raise PersistenceError("Processed timestamp must include a timezone.")
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET processed_text = ?,
                processed_omitted_characters = ?,
                processor_name = ?,
                processor_config_hash = ?,
                processor_model = ?,
                processor_model_digest = ?,
                processed_at = ?
            WHERE id = ?
              AND delivery_status = 'in_flight'
            """,
            (
                text,
                omitted_characters,
                processor_name,
                processor_config_hash,
                processor_model,
                processor_model_digest,
                processed_at.isoformat(),
                record_id,
            ),
            worker_id=worker_id,
        )

    def mark_failed(
        self,
        record_id: int,
        error: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_status = 'pending',
                delivery_attempts = delivery_attempts + 1,
                last_delivery_error = ?,
                claimed_by = '',
                claim_expires_at = NULL
            WHERE id = ?
            """,
            (self._safe_error(error), record_id),
            worker_id=worker_id,
        )

    def mark_dead_letter(
        self,
        record_id: int,
        error: str,
        *,
        worker_id: str | None = None,
    ) -> None:
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_status = 'dead_letter',
                delivery_attempts = delivery_attempts + 1,
                last_delivery_error = ?,
                claimed_by = '',
                claim_expires_at = NULL,
                delivered_at = ?
            WHERE id = ?
            """,
            (self._safe_error(error), datetime.now(UTC).isoformat(), record_id),
            worker_id=worker_id,
        )

    def apply_retention(
        self,
        policy: RetentionPolicy | None = None,
        *,
        now: datetime | None = None,
    ) -> RetentionResult:
        """Delete message bodies Sherlock no longer needs to keep on disk."""
        selected = policy or RetentionPolicy()
        moment = now or datetime.now(UTC)
        self.initialize()
        delivered_removed = 0
        dead_letters_removed = 0
        over_limit_removed = 0
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if selected.delivered_days >= 0:
                        cutoff = moment - timedelta(days=selected.delivered_days)
                        delivered_removed = connection.execute(
                            """
                            DELETE FROM inbound_messages
                            WHERE delivery_status = 'delivered'
                              AND COALESCE(delivered_at, received_at) < ?
                            """,
                            (cutoff.isoformat(),),
                        ).rowcount
                    if selected.dead_letter_days >= 0:
                        cutoff = moment - timedelta(days=selected.dead_letter_days)
                        dead_letters_removed = connection.execute(
                            """
                            DELETE FROM inbound_messages
                            WHERE delivery_status = 'dead_letter'
                              AND COALESCE(delivered_at, received_at) < ?
                            """,
                            (cutoff.isoformat(),),
                        ).rowcount
                    if selected.max_messages > 0:
                        over_limit_removed = self._trim_to_limit(
                            connection,
                            selected.max_messages,
                        )
                except Exception:
                    connection.rollback()
                    raise
                connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError(
                "Cannot apply the message retention policy."
            ) from exc
        return RetentionResult(
            delivered_removed=max(delivered_removed, 0),
            dead_letters_removed=max(dead_letters_removed, 0),
            over_limit_removed=max(over_limit_removed, 0),
        )

    @staticmethod
    def _trim_to_limit(connection: sqlite3.Connection, max_messages: int) -> int:
        """Drop the oldest already-handled rows once the inbox exceeds its cap.

        Undelivered work is never discarded to make room: a full database has to
        fail loudly rather than silently drop messages that were never sent.
        """
        total = connection.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[
            0
        ]
        excess = int(total) - max_messages
        if excess <= 0:
            return 0
        return connection.execute(
            """
            DELETE FROM inbound_messages
            WHERE id IN (
                SELECT id
                FROM inbound_messages
                WHERE delivery_status IN ('delivered', 'dead_letter')
                ORDER BY COALESCE(delivered_at, received_at), id
                LIMIT ?
            )
            """,
            (excess,),
        ).rowcount

    def _update_delivery(
        self,
        record_id: int,
        statement: str,
        parameters: tuple[Any, ...],
        *,
        worker_id: str | None = None,
    ) -> None:
        self.initialize()
        if worker_id is not None:
            statement = f"{statement.rstrip()}\n              AND claimed_by = ?"
            parameters = (*parameters, worker_id)
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(statement, parameters)
                if cursor.rowcount != 1:
                    connection.rollback()
                    raise PersistenceError(
                        "Message database record is missing or claimed by another "
                        f"worker: {record_id}"
                    )
                connection.commit()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot update message delivery state.") from exc

    @staticmethod
    def _safe_error(error: str) -> str:
        safe_error = "".join(
            character if character.isprintable() else " " for character in error
        )
        return " ".join(safe_error.split())[:MAX_STORED_ERROR_CHARACTERS]

    def _stored_messages(
        self,
        rows: list[tuple[Any, ...]],
    ) -> tuple[StoredMessage, ...]:
        try:
            return tuple(self._stored_message(row) for row in rows)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "The message database contains invalid data."
            ) from exc

    @staticmethod
    def _stored_message(row: tuple[Any, ...]) -> StoredMessage:
        metadata = json.loads(row[9])
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        processed_text = row[11]
        processed_omitted = row[12]
        processor_metadata = row[13:17]
        if processed_text is not None and (
            not isinstance(processed_text, str)
            or not processed_text
            or len(processed_text) > MAX_PROCESSED_TEXT_CHARACTERS
        ):
            raise ValueError("processed text must be text or null")
        if (
            isinstance(processed_omitted, bool)
            or not isinstance(processed_omitted, int)
            or processed_omitted < 0
        ):
            raise ValueError(
                "processed omitted characters must be a non-negative integer"
            )
        if any(
            not isinstance(value, str) or len(value) > MAX_PROCESSOR_METADATA_CHARACTERS
            for value in processor_metadata
        ):
            raise ValueError("processor metadata must be text")
        return StoredMessage(
            record_id=row[0],
            message=InboundMessage(
                source=row[1],
                account_id=row[2],
                external_id=row[3],
                conversation_id=row[4],
                sender=row[5],
                subject=row[6],
                body=row[7],
                received_at=datetime.fromisoformat(row[8]),
                metadata=metadata,
            ),
            delivery_attempts=row[10],
            processed_text=processed_text,
            processed_omitted_characters=processed_omitted,
            processor_name=processor_metadata[0],
            processor_config_hash=processor_metadata[1],
            processor_model=processor_metadata[2],
            processor_model_digest=processor_metadata[3],
        )
