"""Domain errors raised by Codehead."""


class CodeheadError(Exception):
    """Base class for Codehead domain errors."""


class ModelError(CodeheadError):
    """Raised when a model request or response cannot be processed."""


class ToolExecutionError(CodeheadError):
    """Raised when a tool cannot be found or executed."""


class AgentAbortedError(CodeheadError):
    """Raised when the agent loop stops before producing a final answer."""


class ContextError(CodeheadError):
    """Raised when context cannot be safely compacted within its budget."""


class StorageError(CodeheadError):
    """Fatal session archive failure; the session must not be continued."""
