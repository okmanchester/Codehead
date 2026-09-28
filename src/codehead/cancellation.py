"""Shared task settlement primitive for owned asynchronous work."""

import asyncio


async def settle[T](task: asyncio.Task[T]) -> T:
    """Wait through repeated outer cancellation without cancelling owned work."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
    return task.result()
