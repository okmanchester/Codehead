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


class ResumeError(CodeheadError):
    """A session does not meet the conditions for safe continuation."""


class ToolCleanupError(CodeheadError):
    """Fatal: a tool could not be confirmed stopped."""


class ToolInternalError(CodeheadError):
    """A tool violated its contract or raised an unexpected exception."""


class ToolCancelled(ToolExecutionError):
    """A running command was stopped; side effects may have occurred."""


class ToolSkipped(ToolExecutionError):
    """Cancellation prevented execution."""
