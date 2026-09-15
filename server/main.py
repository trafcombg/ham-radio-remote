"""Server entry point. Dev: python -m server.main (from the repo root).
Packaged: HAM-Radio-Server.exe, with config.json next to it."""

import asyncio
import json
import logging
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
from server.radio_manager import RadioManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")

UPDATE_CHECK_INTERVAL_S = 24 * 3600


def load_config():
    return json.loads((app_dir(__file__) / "config.json").read_text(encoding="utf-8"))


async def _update_check_loop():
    """Downloads a newer installer automatically when found, but never
    runs it — restarting a live CAT/audio/PTT server mid-session would
    drop active connections. The operator double-clicks the downloaded
    installer whenever it's convenient to apply it (it closes and
    replaces this running server for them at that point)."""
    while True:
        update = await asyncio.to_thread(check_for_update, APP_VERSION, "Server-Setup")
        if update:
            dest = Path(tempfile.gettempdir()) / f"HAM-Radio-Server-Setup-{update['version']}.exe"
            if not dest.exists():
                try:
                    await asyncio.to_thread(urllib.request.urlretrieve, update["download_url"], dest)
                    log.warning(
                        "нова версия %s изтеглена (текуща %s): %s — стартирай го ръчно когато е удобно",
                        update["version"], APP_VERSION, dest,
                    )
                except OSError:
                    log.exception("неуспешно сваляне на новата версия")
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)


async def main():
    cfg = load_config()
    db = await build_db(cfg.get("db"))

    manager = RadioManager(db)
    await manager.load_all()

    amp_manager = AmplifierManager(db, manager)
    await amp_manager.load_all()

    asyncio.create_task(_update_check_loop())

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
