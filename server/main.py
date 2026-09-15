"""Server entry point. Dev: python -m server.main (from the repo root).
Packaged: HAM-Radio-Server.exe, with config.json next to it."""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

import uvicorn

from common.app_paths import app_dir
from common.updater import check_for_update
from common.version import APP_VERSION
from server.admin_api import app as admin_app
from server.amplifier_manager import AmplifierManager
from server.db import build_db
from server.device_registry import watch_devices
from server.radio_manager import RadioManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

UPDATE_CHECK_INTERVAL_S = 24 * 3600


def load_config():
    return json.loads((app_dir(__file__) / "config.json").read_text(encoding="utf-8"))


async def _update_check_loop():
    """Downloads a newer installer automatically when found, but never
    runs it — restarting a live CAT/audio/PTT server mid-session would
    drop active connections. Surfaced in the admin panel (GET /api/update)
    as "нова версия налична" with a button that calls _apply_pending_update
    below — the operator decides when it's convenient to apply it."""
    while True:
        update = await asyncio.to_thread(check_for_update, APP_VERSION, "Server-Setup")
        if update:
            dest = Path(tempfile.gettempdir()) / f"HAM-Radio-Server-Setup-{update['version']}.exe"
            if not dest.exists():
                try:
                    await asyncio.to_thread(urllib.request.urlretrieve, update["download_url"], dest)
                    log.warning(
                        "нова версия %s изтеглена (текуща %s): %s — приложи от admin панела или го стартирай ръчно",
                        update["version"], APP_VERSION, dest,
                    )
                except OSError:
                    log.exception("неуспешно сваляне на новата версия")
                    await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)
                    continue
            admin_app.state.pending_update = {"version": update["version"], "path": str(dest)}
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)


def _apply_pending_update() -> bool:
    """Launches the already-downloaded installer silently and exits this
    process shortly after — the installer's AppMutex handling (see
    packaging/installer/server.iss) waits for us to close, then replaces
    the files. The operator has to start the server again manually
    afterward, same as every other server start (it's deliberately not
    installed as a service — see server.iss)."""
    update = admin_app.state.pending_update
    if not update:
        return False
    subprocess.Popen([update["path"], "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"])
    asyncio.get_event_loop().call_later(1.0, lambda: os._exit(0))
    return True


async def main():
    cfg = load_config()
    db = await build_db(cfg.get("db"))

    manager = RadioManager(db)
    await manager.load_all()

    amp_manager = AmplifierManager(db, manager)
    await amp_manager.load_all()

    admin_app.state.pending_update = None
    admin_app.state.apply_update = _apply_pending_update
    asyncio.create_task(_update_check_loop())
    asyncio.create_task(watch_devices(10.0))

    web_cfg = cfg.get("web", {})
    host, port = web_cfg.get("host", "0.0.0.0"), web_cfg.get("port", 8080)
    admin_app.state.db = db
    admin_app.state.manager = manager
    admin_app.state.amp_manager = amp_manager

    log.info("admin панел на http://%s:%s/", host, port)
    uvicorn_config = uvicorn.Config(admin_app, host=host, port=port, log_level="info")
    await uvicorn.Server(uvicorn_config).serve()


if __name__ == "__main__":
    asyncio.run(main())
