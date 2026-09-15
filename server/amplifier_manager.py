"""Starts/stops/reloads amplifier bridges, and wires CAT mirror + PTT
safety lockout to a linked radio when the admin panel configures one."""

import logging

from server.amplifier_bridge import AmplifierBridge
from server.bridge_manager import BridgeManager
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


class AmplifierManager(BridgeManager):
    log_name = "amp"
    log = log

    def __init__(self, db, radio_manager):
        super().__init__()
        self.db = db
        self.radio_manager = radio_manager  # to wire linked_radio -> RadioBridge

    async def load_all(self):
        for cfg in await self.db.list_amplifier_configs():
            await self._start(cfg)

    async def _start(self, cfg: dict):
        bridge = AmplifierBridge(cfg, build_transport(cfg), self.db)
        self._track(cfg["name"], bridge, bridge.start())

        linked = cfg.get("linked_radio")
        if linked:
            radio_bridge = self.radio_manager.bridges.get(linked)
            if radio_bridge:
                radio_bridge.add_data_observer(bridge.mirror_cat)
                radio_bridge.amp_fault_check = lambda b=bridge: b.fault
            else:
                log.warning("amplifier %s linked to unknown/not-yet-started radio %s", cfg["name"], linked)

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
