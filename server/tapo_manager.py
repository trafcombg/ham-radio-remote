"""Starts/stops/reloads Tapo smart-plug bridges. A plug can optionally
link to a radio or amplifier (see server/tapo_bridge.py's _sync_link) —
takes optional references to those managers so the bridge can read their
current .bridges dict, same reach-into-the-other-manager pattern
AmplifierManager already uses toward RadioManager."""

import logging

from server.bridge_manager import BridgeManager
from server.db import PostgresDb
from server.tapo_bridge import TapoBridge

log = logging.getLogger("tapo_manager")


class TapoManager(BridgeManager):
    log_name = "tapo"
    log = log

    def __init__(self, db, radio_manager=None, amp_manager=None):
        super().__init__()
        self.db = db
        self.radio_manager = radio_manager
        self.amp_manager = amp_manager

    async def load_all(self):
        for cfg in await self.db.list_tapo_configs():
            await self._start(cfg)

    async def _start(self, cfg: dict):
        bridge = TapoBridge(cfg, self.db, self.radio_manager, self.amp_manager)
        self._track(cfg["name"], bridge, bridge.start())

    async def reload(self, cfg: dict):
        await self.stop(cfg["name"])
        if isinstance(self.db, PostgresDb):
            await self.db.upsert_tapo_config(cfg)
        await self._start(cfg)

    async def remove(self, name: str):
        await self.stop(name)
        if isinstance(self.db, PostgresDb):
            await self.db.delete_tapo_config(name)

    def status(self):
        return {name: {"is_on": b.is_on, "online": b.online} for name, b in self.bridges.items()}
