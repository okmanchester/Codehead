import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

import pytest

from codehead.errors import ResumeError, StorageError
from codehead.models import (
    AssistantMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from codehead.session import Session, SQLiteSessionStore
from codehead.session.store import _MESSAGE


@pytest.fixture
def store(tmp_path):
    archive = SQLiteSessionStore(tmp_path / "archive.sqlite3")
    yield archive
    archive.close()


def create(store, tmp_path, messages=()):
    return Session(
        messages,
        store=store,
        workspace=str(tmp_path),
        model="model-a",
        base_url="https://a.test",
    )


def resume(store, tmp_path, session_id, **overrides):
    settings = {
        "workspace": str(tmp_path),
        "model": "model-a",
        "base_url": "https://a.test",
    }
    settings.update(overrides)
    return Session.resume(session_id, store=store, **settings)


def metadata(store, session_id):
    with store._transaction() as connection:
        return connection.execute(
            "SELECT status, model, base_url, token_factor, updated_at FROM sessions WHERE id=?",
            (session_id,),
        ).fetchone()


@pytest.mark.parametrize(
    "messages",
    [
        (),
        (SystemMessage(content="sys"), UserMessage(content="question")),
        (
            SystemMessage(content="sys"),
            UserMessage(content="question"),
            AssistantMessage(content="answer"),
        ),
        (
            SystemMessage(content="sys"),
            UserMessage(content="question"),
            AssistantMessage(
                tool_calls=(
                    ToolCall(id="one", name="read"),
                    ToolCall(id="two", name="read"),
                )
            ),
            ToolMessage(tool_call_id="one", content="data"),
            ToolMessage(tool_call_id="two", content="error", is_error=True),
        ),
    ],
)
def test_normal_exit_reopen_preserves_id_history_and_next_sequence(
    store, tmp_path, messages
):
    session = create(store, tmp_path, messages)
    session.token_factor = 2.4
    session.finish()
    assert metadata(store, session.id)[:4] == (
        "closed",
        "model-a",
        "https://a.test",
        2.4,
    )
    store.close()

    reopened = SQLiteSessionStore(tmp_path / "archive.sqlite3")
    try:
        restored = resume(reopened, tmp_path, session.id)
        assert restored.id == session.id
        assert restored.messages == messages
        assert restored.summary is None
        assert restored.token_factor == 2.4
        assert not restored.model_changed
        assert metadata(reopened, session.id)[0] == "active"
        restored.add(UserMessage(content="continue"))
        assert restored.records[-1] == (
            len(messages) + 1,
            UserMessage(content="continue"),
        )
        assert len(reopened.list_sessions(str(tmp_path))) == 1
        restored.finish()
        restored_again = resume(reopened, tmp_path, session.id)
        assert restored_again.messages == (*messages, UserMessage(content="continue"))
    finally:
        reopened.close()


def test_latest_checkpoint_and_tail_loaded_without_reading_compressed_bodies(
    store, tmp_path
):
    session = create(
        store,
        tmp_path,
        [
            SystemMessage(content="sys"),
            UserMessage(content="large old input"),
            AssistantMessage(content="large old answer"),
            UserMessage(content="current task"),
        ],
    )
    session.compact("first summary", (session.records[0], session.records[-1]))
    session.add(AssistantMessage(content="progress"))
    session.add(UserMessage(content="next task"))
    session.compact("latest summary", (session.records[0], session.records[-1]))
    session.add(AssistantMessage(tool_calls=(ToolCall(id="read", name="read"),)))
    session.add(ToolMessage(tool_call_id="read", content="file"))
    expected = session.records
    session.finish()
    queries = []
    store._connection.set_trace_callback(queries.append)
    with patch.object(_MESSAGE, "validate_json", wraps=_MESSAGE.validate_json) as parse:
        restored = resume(store, tmp_path, session.id)
    assert restored.records == expected
    assert restored.summary == "latest summary"
    assert len(parse.call_args_list) == len(expected)
    assert [
        json.loads(call.args[0])["content"]
        for call in parse.call_args_list
        if "content" in json.loads(call.args[0])
    ] == [message.content for _, message in expected]
    body_queries = [q for q in queries if q.startswith("SELECT sequence, message_json")]
    assert len(body_queries) == 2
    assert "IN (1,6)" in body_queries[0]
    assert "sequence > 6" in body_queries[1]
    restored.add(UserMessage(content="new input"))
    assert restored.records[-1][0] == 9


@pytest.mark.parametrize(
    "overrides",
    [
        {"model": "model-b"},
        {"base_url": "https://b.test"},
        {"model": "model-b", "base_url": "https://b.test"},
    ],
)
def test_changed_model_or_endpoint_resets_factor_and_updates_metadata(
    store, tmp_path, overrides
):
    session = create(store, tmp_path)
    session.token_factor = 3.6
    session.finish()
    restored = resume(store, tmp_path, session.id, **overrides)
    assert restored.token_factor == 1.2
    assert restored.model_changed
    assert metadata(store, session.id)[1:4] == (
        overrides.get("model", "model-a"),
        overrides.get("base_url", "https://a.test"),
        1.2,
    )


def test_list_filters_workspace_and_orders_recent_activity(store, tmp_path):
    first = create(store, tmp_path)
    second = create(store, tmp_path)
    hidden = Session(store=store, workspace=str(tmp_path / "other"), model="hidden")
    first.finish()
    with store._transaction() as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = '2026-01-01 00:00:00' WHERE id=?",
            (second.id,),
        )
        connection.execute(
            "UPDATE sessions SET updated_at = '2026-01-02 00:00:00' WHERE id=?",
            (first.id,),
        )
    listing = store.list_sessions(str(tmp_path / "."))
    assert [row.id for row in listing] == [first.id, second.id]
    assert [row.status for row in listing] == ["closed", "active"]
    assert hidden.id not in [row.id for row in listing]


def test_resume_rejects_missing_wrong_workspace_and_active_without_changing_state(
    store, tmp_path
):
    with pytest.raises(ResumeError, match="not found"):
        resume(store, tmp_path, "missing")
    session = create(store, tmp_path)
    with pytest.raises(ResumeError, match="not closed normally"):
        resume(store, tmp_path, session.id)
    session.finish()
    before = metadata(store, session.id)
    with pytest.raises(ResumeError, match="workspace"):
        resume(store, tmp_path, session.id, workspace=str(tmp_path / "other"))
    assert metadata(store, session.id) == before
    assert resume(store, tmp_path, session.id).id == session.id


def test_closing_resources_does_not_mark_normal_exit(store, tmp_path):
    session = create(store, tmp_path)
    session.close()
    assert metadata(store, session.id)[0] == "active"
    with pytest.raises(ResumeError):
        resume(store, tmp_path, session.id)


def test_finished_instance_cannot_write_even_after_another_instance_resumes(
    store, tmp_path
):
    session = create(store, tmp_path)
    session.finish()
    restored = resume(store, tmp_path, session.id)
    with pytest.raises(ResumeError, match="already exited"):
        session.add(UserMessage(content="stale writer"))
    with pytest.raises(ResumeError):
        session.compact("stale", ())
    with pytest.raises(ResumeError):
        session.finish()
    restored.add(UserMessage(content="new writer"))
    assert restored.records[-1][0] == 1


@pytest.mark.parametrize(
    "history",
    [
        [AssistantMessage(tool_calls=(ToolCall(id="call", name="read"),))],
        [ToolMessage(tool_call_id="orphan", content="result")],
        [
            AssistantMessage(
                tool_calls=(
                    ToolCall(id="call", name="read"),
                    ToolCall(id="call", name="read"),
                )
            )
        ],
        [
            AssistantMessage(tool_calls=(ToolCall(id="call", name="read"),)),
            UserMessage(content="missing result"),
        ],
        [
            AssistantMessage(tool_calls=(ToolCall(id="call", name="read"),)),
            ToolMessage(tool_call_id="call", content="first"),
            ToolMessage(tool_call_id="call", content="duplicate"),
        ],
        [AssistantMessage(content=" ")],
    ],
)
def test_incomplete_or_invalid_history_cannot_finish_or_restore(
    store, tmp_path, history
):
    session = create(store, tmp_path, [SystemMessage(content="sys"), *history])
    before = session.records
    with pytest.raises(ResumeError):
        session.finish()
    assert session.records == before
    assert metadata(store, session.id)[0] == "active"
    # A forged closed flag must not bypass restoration validation.
    with store._transaction() as connection:
        connection.execute(
            "UPDATE sessions SET status='closed' WHERE id=?", (session.id,)
        )
    with pytest.raises(ResumeError):
        resume(store, tmp_path, session.id)
    assert metadata(store, session.id)[0] == "closed"


@pytest.mark.parametrize(
    "change",
    [
        ("through_sequence", 0),
        ("through_sequence", 100),
        ("through_sequence", 1.5),
        ("summary", ""),
        ("retained_sequences_json", "bad json"),
        ("retained_sequences_json", "{}"),
        ("retained_sequences_json", "[]"),
        ("retained_sequences_json", "[1,1]"),
        ("retained_sequences_json", "[4,1]"),
        ("retained_sequences_json", "[true,4]"),
        ("retained_sequences_json", "[1,{}]"),
        ("retained_sequences_json", "[1,5]"),
        ("retained_sequences_json", "[4]"),
    ],
)
def test_corrupt_latest_checkpoint_rolls_back_claim_without_fallback(
    store, tmp_path, change
):
    session = create(
        store,
        tmp_path,
        [
            SystemMessage(content="sys"),
            UserMessage(content="old"),
            AssistantMessage(content="answer"),
            UserMessage(content="latest"),
        ],
    )
    retained = (session.records[0], session.records[-1])
    session.compact("first valid summary", retained)
    session.compact("latest summary", retained)
    session.finish()
    column, value = change
    with store._transaction() as connection:
        connection.execute(
            f"UPDATE checkpoints SET {column}=? WHERE id=(SELECT MAX(id) FROM checkpoints)",
            (value,),
        )
    before = metadata(store, session.id)
    with pytest.raises(ResumeError):
        resume(store, tmp_path, session.id)
    assert metadata(store, session.id) == before
    assert session.summary == "latest summary"


@pytest.mark.parametrize(
    "corruption", ["missing", "invalid_json", "invalid_model", "factor"]
)
def test_invalid_archive_is_rejected(store, tmp_path, corruption):
    session = create(
        store, tmp_path, [SystemMessage(content="sys"), UserMessage(content="hello")]
    )
    session.finish()
    with store._transaction() as connection:
        if corruption == "missing":
            connection.execute(
                "DELETE FROM messages WHERE session_id=? AND sequence=1", (session.id,)
            )
        elif corruption == "factor":
            connection.execute(
                "UPDATE sessions SET token_factor='invalid' WHERE id=?", (session.id,)
            )
        else:
            data = "bad json" if corruption == "invalid_json" else '{"role":"unknown"}'
            connection.execute(
                "UPDATE messages SET message_json=? WHERE session_id=? AND sequence=2",
                (data, session.id),
            )
    with pytest.raises(ResumeError):
        resume(store, tmp_path, session.id)
    assert metadata(store, session.id)[0] == "closed"


@pytest.mark.parametrize("operation", ["finish", "restore"])
def test_failed_commit_keeps_persistent_and_in_memory_state(store, tmp_path, operation):
    session = create(store, tmp_path)
    session.token_factor = 2.4
    if operation == "restore":
        session.finish()
    before = metadata(store, session.id)
    with store._transaction() as connection:
        # The deferred FK lets the UPDATE succeed but fails transaction commit.
        connection.execute(
            "CREATE TABLE commit_failure (session_id TEXT REFERENCES sessions(id) DEFERRABLE INITIALLY DEFERRED)"
        )
        connection.execute("""CREATE TRIGGER reject_state_commit AFTER UPDATE OF status ON sessions
            BEGIN INSERT INTO commit_failure VALUES ('missing-parent'); END""")
    with pytest.raises(StorageError):
        if operation == "finish":
            session.finish()
        else:
            resume(store, tmp_path, session.id)
    assert session._finished == (operation == "restore")
    assert session.token_factor == 2.4
    # The failed store intentionally cannot be reused; inspect with a new reader.
    with closing(sqlite3.connect(tmp_path / "archive.sqlite3")) as reader:
        row = reader.execute(
            "SELECT status, model, base_url, token_factor, updated_at FROM sessions WHERE id=?",
            (session.id,),
        ).fetchone()
        assert row == before
        assert reader.execute("SELECT COUNT(*) FROM commit_failure").fetchone()[0] == 0


def test_two_connections_competing_to_resume_have_one_winner(store, tmp_path):
    session = create(store, tmp_path)
    session.finish()
    barrier = threading.Barrier(2)

    def claim():
        connection = SQLiteSessionStore(tmp_path / "archive.sqlite3")
        try:
            barrier.wait(timeout=5)
            try:
                resume(connection, tmp_path, session.id)
                return "won"
            except ResumeError:
                return "rejected"
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim)
        second = executor.submit(claim)
        assert sorted([first.result(timeout=10), second.result(timeout=10)]) == [
            "rejected",
            "won",
        ]
    assert metadata(store, session.id)[0] == "active"


def test_legacy_schema_migrates_idempotently_without_rewriting_history(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    original = SystemMessage(content="legacy system").model_dump_json()
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, workspace TEXT, model TEXT);
            CREATE TABLE messages (session_id TEXT NOT NULL REFERENCES sessions(id), sequence INTEGER NOT NULL, message_json TEXT NOT NULL, PRIMARY KEY(session_id, sequence));
            CREATE TABLE checkpoints (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, through_sequence INTEGER NOT NULL, summary TEXT NOT NULL, retained_sequences_json TEXT NOT NULL);
        """)
        connection.execute(
            "INSERT INTO sessions (id, workspace, model) VALUES ('old', ?, 'old-model')",
            (str(tmp_path),),
        )
        connection.execute("INSERT INTO messages VALUES ('old', 1, ?)", (original,))
        connection.execute(
            "INSERT INTO checkpoints (session_id, through_sequence, summary, retained_sequences_json) VALUES ('old', 1, 'summary', '[1]')"
        )
        connection.commit()
    for _ in range(2):
        migrated = SQLiteSessionStore(path)
        try:
            (info,) = migrated.list_sessions(str(tmp_path))
            assert (info.id, info.status, info.model) == ("old", "legacy", "old-model")
            with pytest.raises(ResumeError, match="Legacy"):
                resume(migrated, tmp_path, "old")
            with migrated._transaction() as connection:
                assert (
                    connection.execute("SELECT message_json FROM messages").fetchone()[
                        0
                    ]
                    == original
                )
                assert connection.execute(
                    "SELECT summary, retained_sequences_json FROM checkpoints"
                ).fetchone() == ("summary", "[1]")
        finally:
            migrated.close()


def test_failed_schema_migration_rolls_back_all_added_columns(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript("""
            CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at TEXT, workspace TEXT, model TEXT);
            INSERT INTO sessions VALUES ('old', '2026-01-01', 'workspace', 'model');
            CREATE TRIGGER reject_migration BEFORE UPDATE ON sessions BEGIN SELECT RAISE(ABORT, 'migration failed'); END;
        """)
    with pytest.raises(StorageError, match="migration failed"):
        SQLiteSessionStore(path)
    with closing(sqlite3.connect(path)) as connection:
        assert [
            row[1] for row in connection.execute("PRAGMA table_info(sessions)")
        ] == ["id", "created_at", "workspace", "model"]
