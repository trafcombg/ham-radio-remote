"""Starts/stops/reloads antenna switch bridges. Standalone devices, not
tied to any specific radio — access control is its own per-user ACL
(see server/admin_api.py, mirrors the amplifier's)."""

import logging

from server.antenna_switch_bridge import AntennaSwitchBridge
from server.antenna_switch_transport import SerialAntennaSwitchTransport
from server.bridge_manager import BridgeManager
from server.db import PostgresDb

log = logging.getLogger("antenna_switch_manager")


class AntennaSwitchManager(BridgeManager):
    log_name = "switch"
    log = log

    def __init__(self, db):
        super().__init__()
        self.db = db

    async def load_all(self):
        for cfg in await self.db.list_antenna_switch_configs():
            await self._start(cfg)

    async def _start(self, cfg: dict):
        transport = SerialAntennaSwitchTransport(cfg["serial_port"], cfg.get("baud", 9600))
        bridge = AntennaSwitchBridge(cfg, transport, self.db)
        self._track(cfg["name"], bridge, bridge.start())

    async def reload(self, cfg: dict):
        await self.stop(cfg["name"])
        if isinstance(self.db, PostgresDb):
            await self.db.upsert_antenna_switch_config(cfg)
        await self._start(cfg)

    async def remove(self, name: str):
        await self.stop(name)
        if isinstance(self.db, PostgresDb):
            await self.db.delete_antenna_switch_config(name)

    def status(self):
        return {name: {"port": b.port, "device_id": b.device_id} for name, b in self.bridges.items()}
