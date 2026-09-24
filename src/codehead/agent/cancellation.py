"""Turn-owned work remains reachable until cancellation cleanup finishes."""

import asyncio
import threading
from dataclasses import dataclass, field

from codehead.models import ToolResult
from codehead.session.session import ToolExecution


async def settle[T](task: asyncio.Task[T]) -> T:
    """Wait through repeated outer cancellation without cancelling owned work."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()


@dataclass
class TurnControl:
    requested: bool = False
    owner: asyncio.Task | None = None
    operation: asyncio.Task | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    worker: asyncio.Task[ToolResult] | None = None
    execution: ToolExecution | None = None
    cleanup: asyncio.Task[None] | None = None
    denied_targets: set[tuple[str, ...]] = field(default_factory=set)

    def check(self) -> None:
        if self.cancel.is_set():
            raise asyncio.CancelledError
