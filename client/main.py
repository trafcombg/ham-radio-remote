"""Client entry point. Dev: python -m client.main (from the repo root).
Packaged: HAM-Radio-Client.exe, with config.json next to it."""

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from client.session import RadioSession
from client.ui import MainWindow
from common.app_paths import app_dir
from common.updater import check_for_update
from common.version import APP_VERSION

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("client")

UPDATE_CHECK_INTERVAL_S = 6 * 3600


class UpdateState:
    """Shared between the background check loop and the UI — set once,
    read every tick; there's nothing to coordinate beyond that."""

    def __init__(self):
        self.available: dict | None = None  # {"version":..., "download_url":...}


def config_path() -> Path:
    name = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    return app_dir(__file__) / name


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def start_asyncio_thread():
    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()
    return loop


async def _update_check_loop(state: UpdateState):
    while True:
        update = await asyncio.to_thread(check_for_update, APP_VERSION, "Client-Setup")
        if update:
            state.available = update
            log.info("нова клиентска версия налична: %s", update["version"])
        await asyncio.sleep(UPDATE_CHECK_INTERVAL_S)


def main():
    path = config_path()
    cfg = load_config(path)

    loop = start_asyncio_thread()
    session = RadioSession(cfg)
    update_state = UpdateState()
    asyncio.run_coroutine_threadsafe(session.start_status_watchers(), loop)
    asyncio.run_coroutine_threadsafe(_update_check_loop(update_state), loop)

    app = QApplication(sys.argv)
    window = MainWindow(session, loop, cfg, path, update_state)
    window.show()
    exit_code = app.exec()

    future = asyncio.run_coroutine_threadsafe(session.shutdown(), loop)
    future.result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
