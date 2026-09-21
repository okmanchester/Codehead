"""In-memory session history."""

from collections.abc import Iterable

from codehead.session.message import Message


class Session:
    """Store messages for one in-memory conversation."""

    def __init__(self, messages: Iterable[Message] | None = None) -> None:
        self._messages = list(messages or ())

    @property
    def messages(self) -> tuple[Message, ...]:
        """Return an immutable view of the current message sequence."""
        return tuple(self._messages)

    def add(self, message: Message) -> None:
        """Append one message to the session."""
        self._messages.append(message)

    def __len__(self) -> int:
        return len(self._messages)
