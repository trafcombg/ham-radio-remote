"""Client entry point. Run from the repo root: python -m client.main"""

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from client.session import RadioSession
from client.ui import MainWindow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("client")


def config_path() -> Path:
    name = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    return Path(__file__).with_name(name)


def load_config(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def start_asyncio_thread():
    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=runner, daemon=True).start()
    return loop


def main():
    path = config_path()
    cfg = load_config(path)

    loop = start_asyncio_thread()
    session = RadioSession(cfg)
    asyncio.run_coroutine_threadsafe(session.start_status_watchers(), loop)

    app = QApplication(sys.argv)
    window = MainWindow(session, loop, cfg, path)
    window.show()
    exit_code = app.exec()

    future = asyncio.run_coroutine_threadsafe(session.shutdown(), loop)
    future.result(timeout=5)
    loop.call_soon_threadsafe(loop.stop)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
