"""Agent dependency container."""

from dataclasses import dataclass

from codehead.llm.base import LLMProvider
from codehead.tools.registry import ToolRegistry


@dataclass(frozen=True, slots=True)
class Agent:
    """The capabilities and prompt used by an agent loop."""

    provider: LLMProvider
    tools: ToolRegistry
    system_prompt: str
