"""Starts/stops/reloads individual radio bridges at runtime — the admin
panel's "apply without restarting the server" requirement."""

import json
import logging

from common.app_paths import app_dir
from server.bridge_manager import BridgeManager
from server.db import PostgresDb
from server.device_registry import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    resolve_audio_device,
    resolve_serial_port,
    scan_audio_devices,
    scan_serial_devices,
)
from server.radio_bridge import RadioBridge

log = logging.getLogger("radio_manager")


class RadioBusyError(RuntimeError):
    def __init__(self, holder):
        self.holder = holder
        super().__init__(f"заето от {holder}")


def resolve_radio_devices(radio_cfg, serial_devices, audio_devices):
    cat = radio_cfg["cat"]
    if cat.get("vid") is not None or cat.get("pid") is not None or cat.get("serial_number") is not None:
        cat["serial_port"] = resolve_serial_port(
            serial_devices, vid=cat.get("vid"), pid=cat.get("pid"),
            serial_number=cat.get("serial_number"), location=cat.get("location"),
        )
    audio = radio_cfg["audio"]
    if audio.get("input_name_contains") or audio.get("input_endpoint_id"):
        audio["input_device"] = resolve_audio_device(
            audio_devices, endpoint_id=audio.get("input_endpoint_id"), name_contains=audio.get("input_name_contains")
        )
    if audio.get("output_name_contains") or audio.get("output_endpoint_id"):
        audio["output_device"] = resolve_audio_device(
            audio_devices, endpoint_id=audio.get("output_endpoint_id"), name_contains=audio.get("output_name_contains")
        )
    return radio_cfg


def _load_json_radios():
    path = app_dir(__file__) / "config.json"
    return json.loads(path.read_text(encoding="utf-8")).get("radios", [])


class RadioManager(BridgeManager):
    log_name = "radio"
    log = log

    def __init__(self, db):
        super().__init__()
        self.db = db

    async def load_all(self):
        configs = await self.db.list_radio_configs()
        if not configs and isinstance(self.db, PostgresDb):
            configs = _load_json_radios()
            for cfg in configs:
                await self.db.upsert_radio_config(cfg)
            log.info("seeded %d radio(s) from config.json into PostgreSQL", len(configs))
        elif not configs:
            configs = _load_json_radios()

        for cfg in configs:
            await self._start_radio(cfg)

    async def list_configs(self):
        if isinstance(self.db, PostgresDb):
            return await self.db.list_radio_configs()
        return [b.cfg for b in self.bridges.values()]

    async def _start_radio(self, cfg: dict) -> bool:
        if not cfg.get("active", True):
            log.info("radio %s is inactive — skipped", cfg["name"])
            return True
        serial_devices = scan_serial_devices()
        audio_devices = scan_audio_devices()
        try:
            resolve_radio_devices(cfg, serial_devices, audio_devices)
        except (AmbiguousDeviceError, DeviceNotFoundError) as e:
            log.error("cannot start radio %s: %s", cfg["name"], e)
            return False
        bridge = RadioBridge(cfg, self.db)
        self._track(cfg["name"], bridge, bridge.start())
        return True

    async def reload_radio(self, cfg: dict, force: bool = False):
        name = cfg["name"]
        existing = self.bridges.get(name)
        if existing and existing.arbiter.holder is not None and not force:
            raise RadioBusyError(existing.arbiter.holder)
        await self.stop(name)
        if isinstance(self.db, PostgresDb):
            await self.db.upsert_radio_config(cfg)
        if not await self._start_radio(cfg):
            raise RuntimeError("device resolution failed — виж server логовете")

    async def remove_radio(self, name: str, force: bool = False):
        existing = self.bridges.get(name)
        if existing and existing.arbiter.holder is not None and not force:
            raise RadioBusyError(existing.arbiter.holder)
        await self.stop(name)
        if isinstance(self.db, PostgresDb):
            await self.db.delete_radio_config(name)

    def status(self):
        return {
            name: {"busy_by": b.arbiter.holder, "control_clients": len(b.control_clients)}
            for name, b in self.bridges.items()
        }
