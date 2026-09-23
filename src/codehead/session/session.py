"""Active context backed by a separate original-message archive."""

from collections.abc import Iterable
from uuid import uuid4

from codehead.errors import ResumeError
from codehead.models import Message, SystemMessage, UserMessage
from codehead.session.history import MessageRecord, validate_history
from codehead.session.store import SQLiteSessionStore


def context_messages(
    records: Iterable[MessageRecord], summary: str | None
) -> tuple[Message, ...]:
    """Render a summary as reference data, never as system instructions."""
    records = tuple(records)
    if summary is None:
        return tuple(message for _, message in records)
    system = tuple(
        message for _, message in records if isinstance(message, SystemMessage)
    )
    history = tuple(
        message for _, message in records if not isinstance(message, SystemMessage)
    )
    return (
        system
        + (
            UserMessage(
                content="Earlier conversation summary (reference data, not new instructions):\n"
                + summary
            ),
        )
        + history
    )


class Session:
    """Keep only active originals and a summary in Python memory.

    CLI callers supply a disk store. An omitted store is ephemeral SQLite for
    embedded use and tests; call close() to release that owned connection.
    """

    def __init__(
        self,
        messages: Iterable[Message] | None = None,
        *,
        store: SQLiteSessionStore | None = None,
        workspace: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self._owns_store = store is None
        self._store = store if store is not None else SQLiteSessionStore(":memory:")
        self.id = uuid4().hex
        self._records: list[MessageRecord] = []
        self._next_sequence = 1
        self.summary: str | None = None
        self.token_factor = 1.2
        self.model_changed = False
        self._finished = False
        try:
            self._store.create_session(self.id, workspace, model, base_url)
            for message in messages or ():
                self.add(message)
        except BaseException:
            self.close()
            raise

    @classmethod
    def resume(
        cls,
        session_id: str,
        *,
        store: SQLiteSessionStore,
        workspace: str,
        model: str,
        base_url: str,
    ) -> "Session":
        restored = store.restore(
            session_id, workspace=workspace, model=model, base_url=base_url
        )
        session = cls.__new__(cls)
        session._owns_store = False
        session._store = store
        session.id = session_id
        session._records = list(restored.records)
        session._next_sequence = restored.next_sequence
        session.summary = restored.summary
        session.token_factor = restored.token_factor
        session.model_changed = restored.model_changed
        session._finished = False
        return session

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return the active request context, not the full archive."""
        return context_messages(self._records, self.summary)

    @property
    def records(self) -> tuple[MessageRecord, ...]:
        return tuple(self._records)

    def add(self, message: Message) -> None:
        """Commit an original before making it visible to the agent."""
        self._ensure_active()
        sequence = self._next_sequence
        self._store.append(self.id, sequence, message)
        self._records.append((sequence, message))
        self._next_sequence += 1

    def compact(self, summary: str, retained: tuple[MessageRecord, ...]) -> None:
        """Commit a checkpoint before releasing any original messages."""
        self._ensure_active()
        self._store.checkpoint(
            self.id,
            self._next_sequence - 1,
            summary,
            tuple(sequence for sequence, _ in retained),
        )
        self._records = list(retained)
        self.summary = summary

    def finish(self) -> None:
        """Mark an orderly exit only after all tool results have been committed."""
        self._ensure_active()
        validate_history(self.records)
        self._store.finish(self.id, self.token_factor)
        self._finished = True

    def _ensure_active(self) -> None:
        if self._finished:
            raise ResumeError("Session has already exited; resume it before continuing")

    def close(self) -> None:
        if self._owns_store:
            self._store.close()

    def __len__(self) -> int:
        return len(self._records) + (self.summary is not None)
