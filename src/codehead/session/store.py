"""Append-only SQLite archive, separate from the active model context."""

import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from codehead.errors import ResumeError, StorageError
from codehead.models import Message
from codehead.session.history import MessageRecord, validate_history

_MESSAGE = TypeAdapter(Message)
_NOW = "strftime('%Y-%m-%d %H:%M:%f', 'now')"


@dataclass(frozen=True, slots=True)
class SessionInfo:
    id: str
    updated_at: str
    model: str | None
    status: str


@dataclass(frozen=True, slots=True)
class RestoredSession:
    records: tuple[MessageRecord, ...]
    summary: str | None
    next_sequence: int
    token_factor: float
    model_changed: bool


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
            # Serialize schema inspection and migration before either connection
            # acquires a read lock that it would later need to upgrade.
            self._connection.execute("BEGIN IMMEDIATE")
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
                self._migrate(connection)
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
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS turns (
                        id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES sessions(id),
                        status TEXT NOT NULL CHECK(status IN ('running','cancelling','completed','failed','cancelled')),
                        reason TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        finished_at TEXT
                    )
                """)
                connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_live_turn
                    ON turns(session_id) WHERE status IN ('running','cancelling')""")
                connection.execute("""
                    CREATE TABLE IF NOT EXISTS tool_executions (
                        id TEXT PRIMARY KEY,
                        turn_id TEXT NOT NULL REFERENCES turns(id),
                        session_id TEXT NOT NULL,
                        assistant_sequence INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        tool_call_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled','skipped','denied')),
                        result_sequence INTEGER,
                        UNIQUE(turn_id, assistant_sequence, position),
                        UNIQUE(session_id, result_sequence),
                        FOREIGN KEY(session_id, assistant_sequence) REFERENCES messages(session_id, sequence),
                        FOREIGN KEY(session_id, result_sequence) REFERENCES messages(session_id, sequence)
                    )
                """)
                self._migrate_tool_outcomes(connection)
        except (OSError, sqlite3.Error, StorageError) as exc:
            self.close()
            raise StorageError(f"Cannot open session archive: {exc}") from exc

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Add resume metadata atomically; historical exits remain unknown."""
        columns = {row[1] for row in connection.execute("PRAGMA table_info(sessions)")}
        additions = {
            "status": "TEXT NOT NULL DEFAULT 'legacy'",
            "updated_at": "TEXT",
            "token_factor": "REAL NOT NULL DEFAULT 1.2",
            "base_url": "TEXT",
        }
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(
                    f"ALTER TABLE sessions ADD COLUMN {name} {definition}"
                )
        connection.execute(
            "UPDATE sessions SET updated_at = created_at WHERE updated_at IS NULL"
        )

    @staticmethod
    def _migrate_tool_outcomes(connection: sqlite3.Connection) -> None:
        """Rebuild the old CHECK constraint without changing archived executions."""
        schema = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tool_executions'"
        ).fetchone()[0]
        if "'denied'" in schema:
            return
        connection.execute("ALTER TABLE tool_executions RENAME TO tool_executions_old")
        connection.execute("""
            CREATE TABLE tool_executions (
                id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL REFERENCES turns(id),
                session_id TEXT NOT NULL,
                assistant_sequence INTEGER NOT NULL,
                position INTEGER NOT NULL,
                tool_call_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed','cancelled','skipped','denied')),
                result_sequence INTEGER,
                UNIQUE(turn_id, assistant_sequence, position),
                UNIQUE(session_id, result_sequence),
                FOREIGN KEY(session_id, assistant_sequence) REFERENCES messages(session_id, sequence),
                FOREIGN KEY(session_id, result_sequence) REFERENCES messages(session_id, sequence)
            )
        """)
        connection.execute(
            "INSERT INTO tool_executions SELECT * FROM tool_executions_old"
        )
        connection.execute("DROP TABLE tool_executions_old")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        if self._failed or self._connection is None:
            raise StorageError("Session archive is closed or failed")
        try:
            with self._connection:
                yield self._connection
        except sqlite3.Error as exc:
            self._failed = True
            raise StorageError(f"Session archive operation failed: {exc}") from exc

    def create_session(
        self,
        session_id: str,
        workspace: str | None,
        model: str | None,
        base_url: str | None = None,
    ) -> None:
        with self._transaction() as connection:
            connection.execute(
                f"""INSERT INTO sessions
                    (id, workspace, model, base_url, status, updated_at)
                    VALUES (?, ?, ?, ?, 'active', {_NOW})""",
                (session_id, workspace, model, base_url),
            )

    def append(self, session_id: str, sequence: int, message: Message) -> None:
        with self._transaction() as connection:
            self._touch_active(connection, session_id)
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
            self._touch_active(connection, session_id)
            if connection.execute(
                "SELECT 1 FROM tool_executions WHERE session_id = ? "
                "AND status IN ('pending','running')",
                (session_id,),
            ).fetchone():
                raise ResumeError("Cannot checkpoint an incomplete tool group")
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

    @staticmethod
    def _touch_active(connection: sqlite3.Connection, session_id: str) -> None:
        result = connection.execute(
            f"UPDATE sessions SET updated_at = {_NOW} WHERE id = ? AND status = 'active'",
            (session_id,),
        )
        if result.rowcount != 1:
            raise ResumeError("Session is not active")

    def list_sessions(self, workspace: str) -> tuple[SessionInfo, ...]:
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id, updated_at, model, status, workspace FROM sessions "
                "ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        return tuple(
            SessionInfo(*row[:4])
            for row in rows
            if row[4] is not None
            and Path(row[4]).resolve() == Path(workspace).resolve()
        )

    def finish(self, session_id: str, token_factor: float) -> None:
        if not math.isfinite(token_factor) or token_factor < 1.2:
            raise ResumeError("Session has an invalid token calibration factor")
        with self._transaction() as connection:
            self._assert_settled(connection, session_id)
            result = connection.execute(
                f"""UPDATE sessions SET status = 'closed', token_factor = ?,
                    updated_at = {_NOW} WHERE id = ? AND status = 'active'""",
                (token_factor, session_id),
            )
            if result.rowcount != 1:
                raise ResumeError("Session is not active")

    def restore(
        self, session_id: str, *, workspace: str, model: str, base_url: str
    ) -> RestoredSession:
        """Claim and validate in one transaction; failures roll back the claim."""
        with self._transaction() as connection:
            # Take the write lock before reading, avoiding a read-to-write lock
            # upgrade when another process is trying to resume the same session.
            claimed = connection.execute(
                f"UPDATE sessions SET status = 'active', updated_at = {_NOW} "
                "WHERE id = ? AND status = 'closed'",
                (session_id,),
            ).rowcount
            metadata = connection.execute(
                "SELECT workspace, model, base_url, token_factor, status FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            if metadata is None:
                raise ResumeError(f"Session not found: {session_id}")
            if (
                metadata[0] is None
                or Path(metadata[0]).resolve() != Path(workspace).resolve()
            ):
                raise ResumeError(
                    "Session workspace does not match the current workspace"
                )
            if claimed != 1:
                if metadata[4] == "legacy":
                    raise ResumeError(
                        "Legacy session has unknown exit status and cannot be resumed"
                    )
                raise ResumeError(
                    "Session is active or was not closed normally; it cannot be resumed"
                )
            self._assert_settled(connection, session_id)
            records, summary, next_sequence = self._load_context(connection, session_id)
            validate_history(records)
            factor = metadata[3]
            if (
                not isinstance(factor, (int, float))
                or not math.isfinite(factor)
                or factor < 1.2
            ):
                raise ResumeError("Session has an invalid token calibration factor")
            changed = (model, base_url) != (metadata[1], metadata[2])
            factor = 1.2 if changed else factor
            connection.execute(
                "UPDATE sessions SET model = ?, base_url = ?, token_factor = ? WHERE id = ?",
                (model, base_url, factor, session_id),
            )
            restored = RestoredSession(records, summary, next_sequence, factor, changed)
        return restored

    @staticmethod
    def _load_context(
        connection: sqlite3.Connection, session_id: str
    ) -> tuple[tuple[MessageRecord, ...], str | None, int]:
        count, maximum, minimum = connection.execute(
            "SELECT COUNT(*), COALESCE(MAX(sequence), 0), MIN(sequence) "
            "FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if type(maximum) is not int or count != maximum or (count and minimum != 1):
            raise ResumeError("Session archive has invalid message sequences")
        checkpoint = connection.execute(
            "SELECT through_sequence, summary, retained_sequences_json "
            "FROM checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        summary = None
        if checkpoint is None:
            rows = connection.execute(
                "SELECT sequence, message_json FROM messages WHERE session_id = ? ORDER BY sequence",
                (session_id,),
            ).fetchall()
        else:
            boundary, summary, encoded_ids = checkpoint
            try:
                retained = json.loads(encoded_ids)
            except (TypeError, ValueError) as exc:
                raise ResumeError("Invalid checkpoint retained sequences") from exc
            if (
                type(boundary) is not int
                or not 1 <= boundary <= maximum
                or not isinstance(summary, str)
                or not summary.strip()
                or not isinstance(retained, list)
                or not retained
                or any(
                    type(seq) is not int or not 1 <= seq <= boundary for seq in retained
                )
                or retained != sorted(set(retained))
                or retained[0] != 1
            ):
                raise ResumeError(
                    "Invalid checkpoint boundary, summary or retained sequences"
                )
            # Bound placeholders without requiring SQLite JSON extensions.
            rows = []
            for offset in range(0, len(retained), 900):
                batch = retained[offset : offset + 900]
                placeholders = ",".join("?" for _ in batch)
                rows.extend(
                    connection.execute(
                        "SELECT sequence, message_json FROM messages WHERE session_id = ? "
                        f"AND sequence IN ({placeholders}) ORDER BY sequence",
                        (session_id, *batch),
                    ).fetchall()
                )
            if [row[0] for row in rows] != retained:
                raise ResumeError("Checkpoint references missing messages")
            rows += connection.execute(
                "SELECT sequence, message_json FROM messages WHERE session_id = ? "
                "AND sequence > ? ORDER BY sequence",
                (session_id, boundary),
            ).fetchall()
        try:
            records = tuple((seq, _MESSAGE.validate_json(data)) for seq, data in rows)
        except (ValidationError, TypeError, ValueError) as exc:
            raise ResumeError("Session archive contains an invalid message") from exc
        return records, summary, maximum + 1

    @staticmethod
    def _assert_settled(connection: sqlite3.Connection, session_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM turns WHERE session_id = ? AND status IN ('running','cancelling')",
                (session_id,),
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM tool_executions WHERE session_id = ? "
                "AND (status IN ('pending','running') OR result_sequence IS NULL)",
                (session_id,),
            ).fetchone()
        ):
            raise ResumeError("Session has an unsettled turn or tool")

    def begin_turn(
        self, session_id: str, turn_id: str, records: tuple[MessageRecord, ...]
    ) -> None:
        with self._transaction() as connection:
            self._touch_active(connection, session_id)
            self._assert_settled(connection, session_id)
            connection.execute(
                "INSERT INTO turns(id, session_id, status) VALUES (?, ?, 'running')",
                (turn_id, session_id),
            )
            connection.executemany(
                "INSERT INTO messages VALUES (?, ?, ?)",
                [(session_id, seq, msg.model_dump_json()) for seq, msg in records],
            )

    def accept_response(
        self,
        session_id: str,
        turn_id: str,
        sequence: int,
        message: Message,
        executions: list[tuple[str, str]],
    ) -> None:
        with self._transaction() as connection:
            self._touch_active(connection, session_id)
            self._require_turn(connection, turn_id, ("running",))
            self._require_tools_settled(connection, turn_id)
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (session_id, sequence, message.model_dump_json()),
            )
            for position, (execution_id, call_id) in enumerate(executions):
                connection.execute(
                    """INSERT INTO tool_executions
                    (id, turn_id, session_id, assistant_sequence, position, tool_call_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')""",
                    (execution_id, turn_id, session_id, sequence, position, call_id),
                )
            if not executions:
                connection.execute(
                    f"UPDATE turns SET status = 'completed', finished_at = {_NOW} WHERE id = ?",
                    (turn_id,),
                )

    @staticmethod
    def _require_turn(
        connection: sqlite3.Connection, turn_id: str, statuses: tuple[str, ...]
    ) -> None:
        row = connection.execute(
            "SELECT status FROM turns WHERE id = ?", (turn_id,)
        ).fetchone()
        if row is None or row[0] not in statuses:
            raise ResumeError("Turn is not in the required lifecycle state")

    @staticmethod
    def _require_tools_settled(connection: sqlite3.Connection, turn_id: str) -> None:
        if connection.execute(
            "SELECT 1 FROM tool_executions WHERE turn_id = ? AND status IN ('pending','running')",
            (turn_id,),
        ).fetchone():
            raise ResumeError("Turn has unsettled tools")

    def start_tool(self, turn_id: str, execution_id: str) -> None:
        with self._transaction() as connection:
            self._require_turn(connection, turn_id, ("running",))
            if (
                connection.execute(
                    "UPDATE tool_executions SET status = 'running' WHERE id = ? AND turn_id = ? AND status = 'pending'",
                    (execution_id, turn_id),
                ).rowcount
                != 1
            ):
                raise ResumeError("Tool is not pending")

    def complete_tool(
        self,
        session_id: str,
        turn_id: str,
        execution_id: str,
        sequence: int,
        message: Message,
        outcome: str,
    ) -> None:
        with self._transaction() as connection:
            self._touch_active(connection, session_id)
            self._require_turn(connection, turn_id, ("running", "cancelling"))
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?)",
                (session_id, sequence, message.model_dump_json()),
            )
            if (
                connection.execute(
                    "UPDATE tool_executions SET status = ?, result_sequence = ? WHERE id = ? AND turn_id = ? AND status IN ('pending','running')",
                    (outcome, sequence, execution_id, turn_id),
                ).rowcount
                != 1
            ):
                raise ResumeError("Tool result has already been committed")

    def end_turn(self, turn_id: str, status: str, reason: str | None = None) -> None:
        with self._transaction() as connection:
            self._require_turn(connection, turn_id, ("running", "cancelling"))
            if status != "cancelling":
                self._require_tools_settled(connection, turn_id)
            connection.execute(
                f"UPDATE turns SET status = ?, reason = ?, finished_at = CASE WHEN ? = 'cancelling' THEN NULL ELSE {_NOW} END WHERE id = ?",
                (status, reason, status, turn_id),
            )

    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
