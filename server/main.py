"""Server entry point. Run from the repo root: python -m server.main"""

import asyncio
import json
import logging
from pathlib import Path

from common.audio_io import OpusAudioLink
from server.cat_bridge import run_cat_bridge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")


def load_config():
    return json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))


async def main():
    cfg = load_config()

    audio = OpusAudioLink(
        input_device=cfg["audio"]["input_device"],
        output_device=cfg["audio"]["output_device"],
        listen_port=cfg["audio"]["udp_port"],
    )
    audio.start()

    try:
        await run_cat_bridge(
            cfg["cat"]["serial_port"], cfg["cat"]["baud"],
            cfg["cat"]["tcp_host"], cfg["cat"]["tcp_port"],
        )
    finally:
        audio.stop()


if __name__ == "__main__":
    asyncio.run(main())
