"""Server entry point. Run from the repo root: python -m server.main"""

import asyncio
import json
import logging
from pathlib import Path

from server.db import build_db
from server.device_registry import (
    AmbiguousDeviceError,
    DeviceNotFoundError,
    resolve_audio_device,
    resolve_serial_port,
    scan_audio_devices,
    scan_serial_devices,
    watch_devices,
)
from server.radio_bridge import RadioBridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")


def load_config():
    return json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))


def resolve_radio_devices(radio_cfg, serial_devices, audio_devices):
    cat = radio_cfg["cat"]
    if cat.get("vid") is not None or cat.get("pid") is not None or cat.get("serial_number") is not None:
        cat["serial_port"] = resolve_serial_port(
            serial_devices, vid=cat.get("vid"), pid=cat.get("pid"),
            serial_number=cat.get("serial_number"), location=cat.get("location"),
        )
    audio = radio_cfg["audio"]
    if audio.get("name_contains") or audio.get("endpoint_id"):
        idx = resolve_audio_device(
            audio_devices, endpoint_id=audio.get("endpoint_id"), name_contains=audio.get("name_contains")
        )
        audio["input_device"] = idx
        audio["output_device"] = idx
    return radio_cfg


async def main():
    cfg = load_config()
    db = await build_db(cfg.get("db"))

    serial_devices = scan_serial_devices()
    audio_devices = scan_audio_devices()

    bridges = []
    for radio_cfg in cfg["radios"]:
        try:
            resolve_radio_devices(radio_cfg, serial_devices, audio_devices)
        except (AmbiguousDeviceError, DeviceNotFoundError) as e:
            log.error("skipping radio %s: %s", radio_cfg["name"], e)
            continue
        bridges.append(RadioBridge(radio_cfg, db))

    if not bridges:
        log.error("no radios started — check device configuration (see server/list_devices.py)")
        return

    watcher = asyncio.create_task(watch_devices(10.0))
    try:
        await asyncio.gather(*(b.start() for b in bridges))
    finally:
        watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
