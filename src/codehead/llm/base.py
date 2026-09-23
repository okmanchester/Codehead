"""Provider-independent model interface."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from contextlib import aclosing

from codehead.errors import ModelError
from codehead.models import (
    LLMRequest,
    LLMResponse,
    LLMResponseCompleted,
    LLMStreamEvent,
)


class LLMProvider(ABC):
    """Convert conversation state into the next assistant message."""

    @abstractmethod
    def stream(self, request: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        """Yield deltas, then exactly one response. Close on early consumer exit."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Consume the streaming path and return its normalized response."""
        response = None
        async with aclosing(self.stream(request)) as events:
            async for event in events:
                if response is not None:
                    raise ModelError(
                        "Model emitted an event after its complete response"
                    )
                if isinstance(event, LLMResponseCompleted):
                    response = event.response
        if response is None:
            raise ModelError("Model stream ended without a complete response")
        return response

    async def aclose(self) -> None:
        """Release provider-owned resources when the caller is finished."""
