"""Placeholder OpenAI-compatible provider."""

from collections.abc import Sequence

from codehead.errors import ModelError
from codehead.llm.base import LLMProvider
from codehead.session import Message
from codehead.tools.base import ToolSchema


class OpenAIProvider(LLMProvider):
    """Configuration boundary for a future Chat Completions adapter."""

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[ToolSchema],
    ) -> Message:
        raise ModelError("OpenAI provider is not implemented in Codehead v0")
