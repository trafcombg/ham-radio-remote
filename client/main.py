"""Client entry point. Run from the repo root: python -m client.main"""

import asyncio
import json
import logging
import sys
import threading
from pathlib import Path

from PySide6.QtWidgets import QApplication

from client.com_relay import ComRelay
from client.control import ControlClient
from client.cw.straight_key import StraightKeySource
from client.cw.text_source import TextCwSource
from client.rc28 import Rc28Driver
from client.ui import MainWindow
from common.audio_io import AudioLink
from common.cw_link import CwLink

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("client")


def load_config():
    name = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    return json.loads(Path(__file__).with_name(name).read_text(encoding="utf-8"))


def start_asyncio_thread(coro_factories):
    loop = asyncio.new_event_loop()

    def runner():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(asyncio.gather(*(f() for f in coro_factories)))

    threading.Thread(target=runner, daemon=True).start()
    return loop


def main():
    cfg = load_config()

    relay = ComRelay(
        cfg["com"]["local_port"], cfg["com"]["baud"],
        cfg["com"]["server_host"], cfg["com"]["server_port"],
    )
    control = ControlClient(cfg["username"], cfg["control"]["server_host"], cfg["control"]["server_port"])
    coro_factories = [relay.run, control.run]

    rc28_cfg = cfg.get("rc28", {})
    if rc28_cfg.get("enabled"):
        rc28 = Rc28Driver(relay.send_cat, rc28_cfg["civ_address"], rc28_cfg.get("step_hz", 10))
        relay.on_cat_data = rc28.on_cat_reply
        coro_factories.append(rc28.run)

    loop = start_asyncio_thread(coro_factories)

    audio = AudioLink(
        input_device=cfg["audio"]["input_device"],
        output_device=cfg["audio"]["output_device"],
        listen_port=cfg["audio"]["local_port"],
        peer=(cfg["audio"]["server_host"], cfg["audio"]["server_port"]),
    )
    audio.start()

    cw_cfg = cfg["cw"]
    cw_link = CwLink(
        cw_cfg["local_port"],
        peer=(cw_cfg["server_host"], cw_cfg["server_port"]),
        username=cfg["username"],
    )
    straight_key = StraightKeySource(cw_link.send_key)
    text_cw = TextCwSource(cw_cfg["wpm"], cw_link.send_key)

    app = QApplication(sys.argv)
    window = MainWindow(relay, control, loop, audio, cfg["username"], straight_key, text_cw)
    window.show()
    exit_code = app.exec()
    audio.stop()
    cw_link.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
