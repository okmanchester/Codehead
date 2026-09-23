"""Append-only SQLite archive, separate from the active model context."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from codehead.errors import StorageError
from codehead.models import Message


class SQLiteSessionStore:
    """One synchronous connection, owned and closed by its caller."""

    def __init__(self, path: str | Path) -> None:
        self._failed = False
        self._connection: sqlite3.Connection | None = None
        try:
            if str(path) != ":memory:":
                Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._connection = sqlite3.connect(path, autocommit=True)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.autocommit = False
            with self._transaction() as connection:
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS sessions (
                        id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        workspace TEXT,
                        model TEXT
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        sequence INTEGER NOT NULL,
                        message_json TEXT NOT NULL,
                        PRIMARY KEY (session_id, sequence)
                    )
                """)
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS checkpoints (
                        id INTEGER PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        through_sequence INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        retained_sequences_json TEXT NOT NULL
                    )
                """)
        except (OSError, sqlite3.Error, StorageError) as exc:
            self.close()
            raise StorageError(f"Cannot open session archive: {exc}") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._failed or self._connection is None:
            raise StorageError("Session archive is closed or failed")
        try:
            with self._connection:
                yield self._connection
        except sqlite3.Error as exc:
            self._failed = True
            raise StorageError(f"Session archive write failed: {exc}") from exc

    def create_session(
        self, session_id: str, workspace: str | None, model: str | None
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO sessions (id, workspace, model) VALUES (?, ?, ?)",
                (session_id, workspace, model),
            )

    def append(self, session_id: str, sequence: int, message: Message) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (session_id, sequence, message.model_dump_json()),
            )

    def checkpoint(
        self,
        session_id: str,
        through_sequence: int,
        summary: str,
        retained_sequences: tuple[int, ...],
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                """INSERT INTO checkpoints
                   (session_id, through_sequence, summary, retained_sequences_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    session_id,
                    through_sequence,
                    summary,
                    json.dumps(retained_sequences),
                ),
            )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
