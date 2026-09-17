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
        task = asyncio.create_task(self._run(name, bridge, coro), name=f"{self.log_name}:{name}")
        task.add_done_callback(self._log_task_result)
        self.tasks[name] = task

    async def _run(self, name: str, bridge, coro):
        # If the bridge's own connect step fails, it must NOT stay
        # registered in self.bridges — otherwise every future write() call
        # silently no-ops against a dead transport instead of the caller
        # getting a clear "not connected".
        try:
            await coro
        except Exception:
            if self.bridges.get(name) is bridge:
                del self.bridges[name]
            raise

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
            # transport.close() on a serial_asyncio connection only
            # *schedules* the actual port release (loop.call_soon) —
            # without yielding here, an immediate reopen of the same port
            # (reload()) can race it and get "Access is denied" on Windows.
            await asyncio.sleep(0.05)
        task = self.tasks.pop(name, None)
        if task:
            task.cancel()
            # An already-failed task re-raises that same exception here,
            # not CancelledError — already logged via _log_task_result, so
            # it must not blow up the caller's stop()/reload() a second time.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


if __name__ == "__main__":
    class _FakeBridge:
        def __init__(self):
            self.shutdown_called = False

        async def shutdown(self):
            self.shutdown_called = True

    async def _demo():
        mgr = BridgeManager()

        # A bridge whose start() fails must not stay registered — a caller
        # must see "not connected", not a zombie that silently no-ops.
        failing = _FakeBridge()

        async def _boom():
            raise PermissionError("port busy")

        mgr._track("dead", failing, _boom())
        await asyncio.sleep(0)  # let the task run and hit the except branch
        assert "dead" not in mgr.bridges, "failed bridge must be evicted from self.bridges"

        # stop() on that already-failed task must not re-raise its exception.
        await mgr.stop("dead")  # would previously raise PermissionError

        # A bridge whose start() succeeds (and then just idles, like a real
        # long-running bridge) stays registered until stop() is called.
        alive = _FakeBridge()

        async def _idle():
            await asyncio.sleep(10)

        mgr._track("live", alive, _idle())
        await asyncio.sleep(0)
        assert "live" in mgr.bridges
        await mgr.stop("live")
        assert "live" not in mgr.bridges
        assert alive.shutdown_called

    asyncio.run(_demo())
    print("bridge_manager.py: ok")
