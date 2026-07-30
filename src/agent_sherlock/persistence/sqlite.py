from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
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

SCHEMA_VERSION = 2
DATABASE_BUSY_TIMEOUT_MS = 5_000
MAX_STORED_ERROR_CHARACTERS = 1_000

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
        CHECK (delivery_status IN ('pending', 'delivered', 'dead_letter')),
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    last_delivery_error TEXT NOT NULL DEFAULT '',
    delivered_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, account_id, external_id)
);
"""

_CREATE_PENDING_INDEX = """
CREATE INDEX IF NOT EXISTS idx_inbound_pending
ON inbound_messages (delivery_status, received_at, id);
"""


class PersistenceError(RuntimeError):
    """Raised when the durable inbox cannot be read or updated safely."""


@dataclass(frozen=True, slots=True)
class StoredMessage:
    record_id: int
    message: InboundMessage
    delivery_attempts: int


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
                if current_version == 1 or (
                    current_version == 0 and has_messages_table is not None
                ):
                    self._migrate_v1(connection)
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
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        except Exception:
            connection.rollback()
            raise
        connection.commit()

    @staticmethod
    def _migrate_v1(connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP INDEX IF EXISTS idx_inbound_pending")
            connection.execute(
                "ALTER TABLE inbound_messages RENAME TO inbound_messages_v1"
            )
            connection.execute(_CREATE_MESSAGES_TABLE)
            connection.execute(_CREATE_PENDING_INDEX)
            connection.execute(
                """
            INSERT INTO inbound_messages (
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
                delivery_status,
                delivery_attempts,
                last_delivery_error,
                delivered_at,
                created_at
            )
            SELECT
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
                delivery_status,
                delivery_attempts,
                last_delivery_error,
                delivered_at,
                created_at
            FROM inbound_messages_v1;
            """
            )
            connection.execute("DROP TABLE inbound_messages_v1")
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
                for message in messages:
                    metadata = json.dumps(
                        message.metadata,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO inbound_messages (
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
                connection.commit()
        except (TypeError, ValueError, sqlite3.Error) as exc:
            raise PersistenceError("Cannot persist incoming messages.") from exc
        return inserted

    def pending(self, *, limit: int = 100) -> tuple[StoredMessage, ...]:
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                rows = connection.execute(
                    """
                    SELECT
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
                        delivery_attempts
                    FROM inbound_messages
                    WHERE delivery_status = 'pending'
                    ORDER BY received_at, id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        except sqlite3.Error as exc:
            raise PersistenceError("Cannot load pending messages.") from exc

        try:
            return tuple(self._stored_message(row) for row in rows)
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PersistenceError(
                "The message database contains invalid data."
            ) from exc

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

    def mark_delivered(self, record_id: int, delivered_at: datetime) -> None:
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_status = 'delivered',
                delivery_attempts = delivery_attempts + 1,
                last_delivery_error = '',
                delivered_at = ?
            WHERE id = ?
            """,
            (delivered_at.isoformat(), record_id),
        )

    def mark_failed(self, record_id: int, error: str) -> None:
        safe_error = self._safe_error(error)
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_attempts = delivery_attempts + 1,
                last_delivery_error = ?
            WHERE id = ?
            """,
            (safe_error, record_id),
        )

    def mark_dead_letter(self, record_id: int, error: str) -> None:
        self._update_delivery(
            record_id,
            """
            UPDATE inbound_messages
            SET delivery_status = 'dead_letter',
                delivery_attempts = delivery_attempts + 1,
                last_delivery_error = ?
            WHERE id = ?
            """,
            (self._safe_error(error), record_id),
        )

    def _update_delivery(
        self,
        record_id: int,
        statement: str,
        parameters: tuple[Any, ...],
    ) -> None:
        self.initialize()
        try:
            with closing(self._connect()) as connection:
                cursor = connection.execute(statement, parameters)
                if cursor.rowcount != 1:
                    raise PersistenceError(
                        f"Message database record does not exist: {record_id}"
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

    @staticmethod
    def _stored_message(row: tuple[Any, ...]) -> StoredMessage:
        metadata = json.loads(row[9])
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
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
        )
