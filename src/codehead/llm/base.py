"""Provider-independent model interface."""

from abc import ABC, abstractmethod
from collections.abc import Sequence

from codehead.session import Message
from codehead.tools.base import ToolSchema


class LLMProvider(ABC):
    """Convert conversation state into the next assistant message."""

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> Message:
        """Return one assistant response for the supplied conversation."""
