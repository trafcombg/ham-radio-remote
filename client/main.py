"""Client entry point. Dev: python -m client.main (from the repo root).
Packaged: HAM-Radio-Client.exe, with config.json next to it."""

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from client import credential_store
from client.login_dialog import LoginDialog
from client.session import RadioSession
from client.ui import MainWindow
from common.app_paths import app_dir
from common.firewall import ensure_ports_open
from common.priority import raise_process_priority
from common.updater import check_for_update
from common.version import APP_VERSION

def _setup_logging():
    handlers = [logging.FileHandler(app_dir(__file__) / "client.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


_setup_logging()
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


def _login(cfg: dict, path: Path) -> str | None:
    """Shows the startup login dialog, pre-filled from the saved
    (encrypted) password if any; migrates a legacy plaintext "password"
    field to encrypted storage on save. Returns the plaintext password to
    use this session, or None if the user cancelled."""
    encrypted = cfg.get("password_encrypted", "")
    stored_password = credential_store.decrypt(encrypted) if encrypted else cfg.get("password", "")
    dialog = LoginDialog(cfg.get("username", ""), stored_password)
    if not dialog.exec():
        return None
    username, password = dialog.result_credentials()
    cfg["username"] = username
    cfg.pop("password", None)
    cfg["password_encrypted"] = credential_store.encrypt(password)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    return password


def main():
    raise_process_priority()
    path = config_path()
    cfg = load_config(path)

    app = QApplication(sys.argv)
    password = _login(cfg, path)
    if password is None:
        sys.exit(0)

    loop = start_asyncio_thread()
    session = RadioSession(cfg, path)
    session.password = password
    update_state = UpdateState()
    try:
        asyncio.run_coroutine_threadsafe(session.refresh_radios(), loop).result(timeout=10)
    except Exception:
        log.warning("не успях да взема списъка с радиа от сървъра при старт", exc_info=True)
    asyncio.run_coroutine_threadsafe(session.start_amplifier_poll(), loop)
    asyncio.run_coroutine_threadsafe(session.start_antenna_switch_poll(), loop)
    asyncio.run_coroutine_threadsafe(session.start_cat_busy_poll(), loop)
    asyncio.run_coroutine_threadsafe(_update_check_loop(update_state), loop)

    ports = [(cfg["audio"]["local_port"], "udp"), (cfg["cw"]["local_port"], "udp")]
    asyncio.run_coroutine_threadsafe(asyncio.to_thread(ensure_ports_open, ports, "HAM Radio Remote Client"), loop)

    window = MainWindow(session, loop, cfg, path, update_state)
    window.show()
    exit_code = app.exec()

    future = asyncio.run_coroutine_threadsafe(session.shutdown(), loop)
    try:
        future.result(timeout=5)
    except Exception:
        log.warning("спирането отне твърде дълго / завърши с грешка", exc_info=True)
    loop.call_soon_threadsafe(loop.stop)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
