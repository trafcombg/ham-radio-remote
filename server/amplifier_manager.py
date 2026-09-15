"""Starts/stops/reloads amplifier bridges, and wires CAT mirror + PTT
safety lockout to a linked radio when the admin panel configures one."""

import asyncio
import contextlib
import logging

from server.amplifier_bridge import AmplifierBridge
from server.db import PostgresDb
from server.ebox_transport import HttpEboxTransport, RawTcpAcomTransport, SerialAcomTransport

log = logging.getLogger("amplifier_manager")


def build_transport(cfg: dict):
    kind = cfg["transport"]
    if kind == "serial":
        return SerialAcomTransport(cfg["serial_port"])
    if kind == "tcp":
        return RawTcpAcomTransport(cfg["host"], cfg["port"])
    if kind == "http":
        return HttpEboxTransport(f"http://{cfg['host']}:{cfg.get('port', 80)}", cfg.get("username"), cfg.get("password"))
    raise ValueError(f"unknown amplifier transport: {kind}")


class AmplifierManager:
    def __init__(self, db, radio_manager):
        self.db = db
        self.radio_manager = radio_manager  # to wire linked_radio -> RadioBridge
        self.bridges: dict[str, AmplifierBridge] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    async def load_all(self):
        for cfg in await self.db.list_amplifier_configs():
            await self._start(cfg)

    async def _start(self, cfg: dict):
        bridge = AmplifierBridge(cfg, build_transport(cfg), self.db)
        self.bridges[cfg["name"]] = bridge
        task = asyncio.create_task(bridge.start(), name=f"amp:{cfg['name']}")
        task.add_done_callback(self._log_task_result)
        self.tasks[cfg["name"]] = task

        linked = cfg.get("linked_radio")
        if linked:
            radio_bridge = self.radio_manager.bridges.get(linked)
            if radio_bridge:
                radio_bridge.add_data_observer(bridge.mirror_cat)
                radio_bridge.amp_fault_check = lambda b=bridge: b.fault
            else:
                log.warning("amplifier %s linked to unknown/not-yet-started radio %s", cfg["name"], linked)

    def _log_task_result(self, task: asyncio.Task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            log.error("amplifier task %s failed: %s", task.get_name(), exc, exc_info=exc)

    async def stop(self, name: str):
        bridge = self.bridges.pop(name, None)
        if bridge:
            await bridge.shutdown()
        task = self.tasks.pop(name, None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def reload(self, cfg: dict):
        await self.stop(cfg["name"])
        if isinstance(self.db, PostgresDb):
            await self.db.upsert_amplifier_config(cfg)
        await self._start(cfg)

    async def remove(self, name: str):
        await self.stop(name)
        if isinstance(self.db, PostgresDb):
            await self.db.delete_amplifier_config(name)

    def status(self):
        return {name: (b.latest or {}) for name, b in self.bridges.items()}
