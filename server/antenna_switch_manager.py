"""Starts/stops/reloads antenna switch bridges. Each switch is always
tied to exactly one radio (linked_radio is required, unlike the
amplifier's optional one) — used both for CAT-less "is this radio
currently transmitting" safety checks (see is_radio_transmitting) and
for access control (see server/admin_api.py's reuse of
user_can_access_radio)."""

import logging

from server.antenna_switch_bridge import AntennaSwitchBridge
from server.antenna_switch_transport import SerialAntennaSwitchTransport
from server.bridge_manager import BridgeManager
from server.db import PostgresDb

log = logging.getLogger("antenna_switch_manager")


class AntennaSwitchManager(BridgeManager):
    log_name = "switch"
    log = log

    def __init__(self, db, radio_manager):
        super().__init__()
        self.db = db
        self.radio_manager = radio_manager  # to check the linked radio's PTT arbiter before switching

    async def load_all(self):
        for cfg in await self.db.list_antenna_switch_configs():
            await self._start(cfg)

    async def _start(self, cfg: dict):
        bridge = AntennaSwitchBridge(cfg, SerialAntennaSwitchTransport(cfg["serial_port"]), self.db)
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

    def is_radio_transmitting(self, radio_name: str) -> bool:
        """Switching antenna port while the linked radio is keyed risks
        arcing the switch's relay contacts (and possibly the
        transmitter's output stage) — the same physical hazard PTT
        arbitration already tracks, so this just reads that state
        rather than adding a second lock."""
        bridge = self.radio_manager.bridges.get(radio_name)
        return bool(bridge and bridge.arbiter.holder is not None)
