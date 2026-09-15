"""Server entry point. Run from the repo root: python -m server.main"""

import asyncio
import json
import logging
from pathlib import Path

import uvicorn

from server.admin_api import app as admin_app
from server.amplifier_manager import AmplifierManager
from server.db import build_db
from server.radio_manager import RadioManager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("server")


def load_config():
    return json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))


async def main():
    cfg = load_config()
    db = await build_db(cfg.get("db"))

    manager = RadioManager(db)
    await manager.load_all()

    amp_manager = AmplifierManager(db, manager)
    await amp_manager.load_all()

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
