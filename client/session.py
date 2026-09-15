"""Owns the live connections to whichever radio is currently selected,
plus a lightweight status-only connection to every OTHER radio in the
list (so the radio picker can show "free"/"busy by X" for all of them,
not just the active one).

Switching radios tears down and rebuilds the active bundle against the
new radio's ports. The local COM port, audio device, and CW/RC-28 target
all follow automatically — third-party CAT software (WSJT-X etc.) stays
pointed at the same local COM port throughout, it never needs
reconfiguring when the operator switches radios.

ponytail: the local virtual COM port (and the CW paddle/WinKeyer, if
configured) are closed and reopened on every switch — simplest correct
implementation; com0com pairs tolerate a brief gap fine. Keep hardware
open continuously across switches if that proves disruptive in practice.
"""

import asyncio
import contextlib
import logging

from client.com_relay import ComRelay
from client.control import ControlClient
from client.cw.iambic import IambicKeyer
from client.cw.serial_paddle import SerialPaddle
from client.cw.text_source import TextCwSource
from client.cw.winkeyer import WinkeyerSource
from client.rc28 import Rc28Driver
from common.audio_io import AudioLink
from common.cw_link import CwLink

log = logging.getLogger("session")


class RadioSession:
    def __init__(self, app_cfg: dict):
        self.app_cfg = app_cfg
        self.username = app_cfg["username"]
        self.server_host = app_cfg["server_host"]

        self.status_clients: dict = {}  # radio name -> ControlClient, for the radio picker

        self.radio_name = None
        self.relay = None
        self.control = None
        self.audio = None
        self.cw_link = None
        self.text_cw = None
        self.iambic_keyer = None
        self.paddle = None
        self.winkeyer = None
        self.rc28 = None
        self._tasks = []

    async def start_status_watchers(self):
        for radio_cfg in self.app_cfg["radios"]:
            client = ControlClient(self.username, self.server_host, radio_cfg["control_port"])
            self.status_clients[radio_cfg["name"]] = client
            asyncio.create_task(client.run())

    def radio_status(self, name: str):
        client = self.status_clients.get(name)
        return client.busy_by if client else None

    async def switch_to(self, radio_cfg: dict):
        await self._teardown()
        self.radio_name = radio_cfg["name"]

        com_cfg = self.app_cfg["com"]
        self.relay = ComRelay(com_cfg["local_port"], com_cfg["baud"], self.server_host, radio_cfg["cat_port"])
        self.control = ControlClient(self.username, self.server_host, radio_cfg["control_port"])
        self._tasks.append(asyncio.create_task(self.relay.run()))
        self._tasks.append(asyncio.create_task(self.control.run()))

        audio_cfg = self.app_cfg["audio"]
        self.audio = AudioLink(
            input_device=audio_cfg.get("input_device"),
            output_device=audio_cfg.get("output_device"),
            listen_port=audio_cfg["local_port"],
            peer=(self.server_host, radio_cfg["audio_port"]),
        )
        self.audio.start()

        cw_cfg = self.app_cfg["cw"]
        self.cw_link = CwLink(cw_cfg["local_port"], peer=(self.server_host, radio_cfg["cw_port"]), username=self.username)
        self.text_cw = TextCwSource(cw_cfg["wpm"], self.cw_link.send_key)

        source = cw_cfg.get("source", "straight")
        if source == "iambic" and cw_cfg.get("paddle_port"):
            self.paddle = SerialPaddle(
                cw_cfg["paddle_port"], cw_cfg.get("paddle_dit_pin", "cts"), cw_cfg.get("paddle_dah_pin", "dsr")
            )
            self.iambic_keyer = IambicKeyer(cw_cfg["wpm"], self.cw_link.send_key, self.paddle)
            self._tasks.append(asyncio.create_task(self.iambic_keyer.run()))
        elif source == "winkeyer" and cw_cfg.get("winkeyer_port"):
            self.winkeyer = WinkeyerSource(cw_cfg["winkeyer_port"], cw_cfg.get("winkeyer_baud", 1200), self.cw_link.send_key)
            self._tasks.append(asyncio.create_task(self.winkeyer.run()))

        rc28_cfg = self.app_cfg.get("rc28", {})
        if rc28_cfg.get("enabled") and radio_cfg.get("civ_address"):
            self.rc28 = Rc28Driver(self.relay.send_cat, radio_cfg["civ_address"], rc28_cfg.get("step_hz", 10))
            self.relay.on_cat_data = self.rc28.on_cat_reply
            self._tasks.append(asyncio.create_task(self.rc28.run()))
        else:
            self.rc28 = None

        log.info("switched to radio %s", self.radio_name)

    async def _teardown(self):
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._tasks = []
        if self.audio:
            self.audio.stop()
            self.audio = None
        if self.cw_link:
            self.cw_link.close()
            self.cw_link = None
        if self.iambic_keyer:
            self.iambic_keyer.stop()
            self.iambic_keyer = None
        if self.paddle:
            self.paddle.close()
            self.paddle = None
        if self.winkeyer:
            self.winkeyer.stop()
            self.winkeyer = None

    async def shutdown(self):
        await self._teardown()
        for client in self.status_clients.values():
            if client.writer:
                client.writer.close()
