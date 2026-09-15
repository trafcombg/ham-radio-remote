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
from client.ui import MainWindow
from common.audio_io import OpusAudioLink

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
    loop = start_asyncio_thread([relay.run, control.run])

    audio = OpusAudioLink(
        input_device=cfg["audio"]["input_device"],
        output_device=cfg["audio"]["output_device"],
        listen_port=cfg["audio"]["local_port"],
        peer=(cfg["audio"]["server_host"], cfg["audio"]["server_port"]),
    )
    audio.start()

    app = QApplication(sys.argv)
    window = MainWindow(relay, control, loop, audio, cfg["username"])
    window.show()
    exit_code = app.exec()
    audio.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
