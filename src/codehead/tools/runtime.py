"""One boundary for tool execution, errors, and model-visible output size."""

from copy import deepcopy

from codehead.errors import ToolExecutionError
from codehead.models import ToolCall, ToolResult
from codehead.tools.output import TRUNCATION_MARKER, truncate_text
from codehead.tools.registry import ToolRegistry


class ToolRuntime:
    """Execute a registered tool and return a bounded result for the session."""

    def __init__(self, registry: ToolRegistry, max_output_bytes: int = 50 * 1024):
        if not isinstance(registry, ToolRegistry):
            raise TypeError("registry must be a ToolRegistry")
        if isinstance(max_output_bytes, bool) or not isinstance(max_output_bytes, int):
            raise TypeError("max_output_bytes must be an integer")
        if max_output_bytes < len(TRUNCATION_MARKER.encode("utf-8")) + 2:
            raise ValueError("max_output_bytes is too small for a truncation marker")
        self._registry = registry
        self._max_output_bytes = max_output_bytes

    def execute(self, call: ToolCall) -> ToolResult:
        """Convert tool failures to results; preserve process-level interrupts."""
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        try:
            tool = self._registry.get(call.name)
            output = tool.execute(deepcopy(call.arguments))
            if not isinstance(output, str):
                raise ToolExecutionError(f"Tool '{call.name}' returned non-text output")
            is_error = False
        except ToolExecutionError as exc:
            output = str(exc)
            is_error = True
        except Exception as exc:  # noqa: BLE001 - tool failures become model-visible results
            output = f"Tool '{call.name}' failed: {exc}"
            is_error = True

        return ToolResult(
            tool_call_id=call.id,
            output=truncate_text(output, self._max_output_bytes),
            is_error=is_error,
        )
