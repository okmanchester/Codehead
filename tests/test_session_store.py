import json
import sqlite3

import pytest
from pydantic import TypeAdapter

from codehead.errors import StorageError
from codehead.models import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolCall,
    ToolMessage,
    UserMessage,
)
from codehead.session import Session, SQLiteSessionStore


@pytest.fixture
def store(tmp_path):
    archive = SQLiteSessionStore(tmp_path / "nested" / "sessions.sqlite3")
    yield archive
    archive.close()


def test_originals_round_trip_isolation_and_checkpoints(store, tmp_path):
    originals = (
        SystemMessage(content="instructions"),
        UserMessage(content="处理文件"),
        AssistantMessage(
            tool_calls=(
                ToolCall(id="call", name="read", arguments={"path": "文件.py"}),
            )
        ),
        ToolMessage(tool_call_id="call", content="failed", is_error=True),
        AssistantMessage(content="done"),
    )
    session = Session(originals, store=store, workspace="workspace", model="model")
    other = Session([UserMessage(content="other")], store=store)
    retained = (session.records[0], session.records[-1])
    session.compact("a summary", retained)
    session.add(UserMessage(content="next"))
    assert session.records == (*retained, (6, UserMessage(content="next")))
    assert [message.role for message in session.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert other.messages == (UserMessage(content="other"),)
    store.close()

    with sqlite3.connect(tmp_path / "nested" / "sessions.sqlite3") as reader:
        rows = reader.execute(
            "SELECT sequence, message_json FROM messages WHERE session_id=? ORDER BY sequence",
            (session.id,),
        ).fetchall()
        assert [row[0] for row in rows] == list(range(1, 7))
        assert tuple(TypeAdapter(Message).validate_json(row[1]) for row in rows) == (
            *originals,
            UserMessage(content="next"),
        )
        checkpoint = reader.execute(
            "SELECT through_sequence, summary, retained_sequences_json FROM checkpoints WHERE session_id=?",
            (session.id,),
        ).fetchone()
        assert checkpoint[:2] == (5, "a summary")
        assert json.loads(checkpoint[2]) == [1, 5]
        assert reader.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 2
        assert reader.execute(
            "SELECT workspace, model FROM sessions WHERE id=?", (session.id,)
        ).fetchone() == ("workspace", "model")


@pytest.mark.parametrize("operation", ["message", "checkpoint"])
def test_failed_transaction_preserves_active_context_and_archive(store, operation):
    session = Session(
        [SystemMessage(content="sys"), UserMessage(content="old")], store=store
    )
    before = session.records
    table = "messages" if operation == "message" else "checkpoints"
    with store._connection:
        store._connection.execute(f"""CREATE TRIGGER reject_write BEFORE INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
    with pytest.raises(StorageError, match="injected failure"):
        if operation == "message":
            session.add(UserMessage(content="not committed"))
        else:
            session.compact("not committed", before[:1])
    assert session.records == before
    assert session.summary is None
    assert store._connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    assert (
        store._connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 0
    )
    with pytest.raises(StorageError, match="closed or failed"):
        session.add(UserMessage(content="cannot continue"))


def test_store_open_failure_is_domain_error(tmp_path):
    occupied = tmp_path / "file"
    occupied.write_text("not a directory")
    with pytest.raises(StorageError, match="Cannot open"):
        SQLiteSessionStore(occupied / "archive.sqlite3")


def test_borrowed_store_lifetime_is_owned_by_caller(store):
    session = Session(store=store)
    session.close()
    Session([UserMessage(content="still open")], store=store)
    store.close()
    store.close()
    with pytest.raises(StorageError):
        session.add(UserMessage(content="closed"))
