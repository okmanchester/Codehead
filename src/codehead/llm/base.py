"""Provider-independent model interface."""

from abc import ABC, abstractmethod

from codehead.models import LLMRequest, LLMResponse


class LLMProvider(ABC):
    """Convert conversation state into the next assistant message."""

    @abstractmethod
    def complete(self, request: LLMRequest) -> LLMResponse:
        """Return one normalized response for the supplied request."""
