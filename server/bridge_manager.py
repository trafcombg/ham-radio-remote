"""Shared start/stop/task-tracking for a named collection of bridges (one
per radio or amplifier) — used by RadioManager and AmplifierManager."""

import asyncio
import contextlib
import logging


class BridgeManager:
    log_name = "bridge"  # subclass sets this — used in task names and log lines
    log = logging.getLogger("bridge_manager")

    def __init__(self):
        self.bridges: dict = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def _track(self, name: str, bridge, coro):
        self.bridges[name] = bridge
        task = asyncio.create_task(coro, name=f"{self.log_name}:{name}")
        task.add_done_callback(self._log_task_result)
        self.tasks[name] = task

    def _log_task_result(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            self.log.error("%s task %s failed: %s", self.log_name, task.get_name(), exc, exc_info=exc)

    async def stop(self, name: str):
        bridge = self.bridges.pop(name, None)
        if bridge:
            await bridge.shutdown()
        task = self.tasks.pop(name, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
