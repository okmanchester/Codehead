"""One boundary for tool execution, errors, and model-visible output size."""

import asyncio
from copy import deepcopy
from threading import Event

from codehead.errors import (
    ToolCancelled,
    ToolCleanupError,
    ToolExecutionError,
    ToolSkipped,
)
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

    def start(self, call: ToolCall, cancel: Event) -> asyncio.Task[ToolResult]:
        """Return an owned worker; callers must shield and settle it before exit."""
        return asyncio.create_task(
            asyncio.to_thread(self._execute_worker, call, cancel)
        )

    async def execute_async(self, call: ToolCall, cancel: Event) -> ToolResult:
        from codehead.agent.cancellation import settle

        worker = self.start(call, cancel)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancel.set()
            await settle(worker)
            raise

    def _execute_worker(self, call: ToolCall, cancel: Event) -> ToolResult:
        try:
            return self.execute(call, cancel)
        except BaseException as exc:
            cancel.set()
            raise ToolCleanupError(
                "Tool interrupted without a confirmed result"
            ) from exc

    def execute(self, call: ToolCall, cancel: Event | None = None) -> ToolResult:
        """Convert tool failures to results; preserve process-level interrupts."""
        if not isinstance(call, ToolCall):
            raise TypeError("call must be a ToolCall")
        try:
            tool = self._registry.get(call.name)
            output = (
                tool.execute(deepcopy(call.arguments))
                if cancel is None
                else tool.execute_cancellable(deepcopy(call.arguments), cancel)
            )
            if not isinstance(output, str):
                raise ToolExecutionError(f"Tool '{call.name}' returned non-text output")
            outcome = "succeeded"
        except ToolCleanupError:
            raise
        except (ToolCancelled, ToolSkipped) as exc:
            output = str(exc)
            outcome = "cancelled" if isinstance(exc, ToolCancelled) else "skipped"
        except ToolExecutionError as exc:
            output = str(exc)
            outcome = "failed"
        except Exception as exc:  # noqa: BLE001 - tool failures become model-visible results
            output = f"Tool '{call.name}' failed: {exc}"
            outcome = "failed"

        return ToolResult(
            tool_call_id=call.id,
            output=truncate_text(output, self._max_output_bytes),
            outcome=outcome,
        )
