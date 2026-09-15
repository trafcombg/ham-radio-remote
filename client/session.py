"""Owns the live connections to whichever radio is currently selected,
plus a lightweight status-only connection to every OTHER radio in the
list (so the radio picker can show "free"/"busy by X" for all of them,
not just the active one).

Switching radios tears down and rebuilds the audio/PTT/CW bundle against
the new radio (those share physical hardware — one mic, one speaker, one
operator — so only one radio can be "active" for them at a time). CAT is
different: every active radio gets its own always-on virtual COM port
(see client/com0com.py) so separate local programs (WSJT-X, logging
software, ...) can each stay pointed at a different radio simultaneously,
independent of which radio is selected here.

ponytail: the CW paddle/WinKeyer are closed and reopened on every switch —
simplest correct implementation. Keep hardware open continuously across
switches if that proves disruptive in practice.
"""

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request

from client import com0com
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

RADIOS_FETCH_TIMEOUT_S = 5


class RadioSession:
    def __init__(self, app_cfg: dict, config_path=None):
        self.app_cfg = app_cfg
        self.config_path = config_path
        self.username = app_cfg["username"]
        self.server_host = app_cfg["server_host"]

        self.status_clients: dict = {}  # radio name -> ControlClient, for the radio picker
        self.cat_relays: dict = {}      # radio name -> ComRelay, one per ACTIVE radio, always on
        self._cat_tasks: dict = {}

        self.radio_name = None
        self.control = None
        self.audio = None
        self.cw_link = None
        self.text_cw = None
        self.iambic_keyer = None
        self.paddle = None
        self.winkeyer = None
        self.rc28 = None
        self._tasks = []

    def _fetch_radios(self) -> list:
        url = f"http://{self.server_host}:{self.app_cfg.get('server_api_port', 8080)}/api/client/radios"
        with urllib.request.urlopen(url, timeout=RADIOS_FETCH_TIMEOUT_S) as resp:
            return json.loads(resp.read())["radios"]

    async def refresh_radios(self) -> list:
        """Pulls the current radio list (ports + active flag) from the
        server and (re)starts the status watchers and per-radio CAT
        relays against it. Falls back to whatever's cached in
        app_cfg["radios"] if the server can't be reached, so the client
        still comes up (with possibly-stale ports) when offline."""
        try:
            radios = await asyncio.to_thread(self._fetch_radios)
            self.app_cfg["radios"] = radios
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("не успях да взема списъка с радиа от сървъра (%s) — ползвам локалния списък", e)
            radios = self.app_cfg.get("radios", [])
        await self.start_status_watchers()
        await self.start_cat_relays(radios)
        return radios

    async def start_status_watchers(self):
        for client in self.status_clients.values():
            if client.writer:
                client.writer.close()
        self.status_clients = {}
        for radio_cfg in self.app_cfg.get("radios", []):
            if not radio_cfg.get("active", True):
                continue
            client = ControlClient(self.username, self.server_host, radio_cfg["control_port"])
            self.status_clients[radio_cfg["name"]] = client
            asyncio.create_task(client.run())

    async def start_cat_relays(self, radios: list):
        active_names = {r["name"] for r in radios if r.get("active", True)}
        for name in list(self.cat_relays):
            if name not in active_names:
                self._cat_tasks.pop(name).cancel()
                del self.cat_relays[name]

        try:
            setupc = com0com.find_setupc()
        except com0com.Com0comNotFound as e:
            log.warning("%s", e)
            return

        com_ports = self.app_cfg.setdefault("com_ports", {})
        changed = False
        for radio in radios:
            name = radio["name"]
            if name not in active_names or name in self.cat_relays:
                continue
            mapping = com_ports.get(name)
            if not mapping:
                try:
                    exposed, internal = com0com.create_pair(setupc)
                except Exception:
                    log.exception("не успях да създам виртуален COM порт за %s", name)
                    continue
                mapping = {"local": exposed, "internal": internal}
                com_ports[name] = mapping
                changed = True
            relay = ComRelay(mapping["internal"], self.app_cfg["com"].get("baud", 19200), self.server_host, radio["cat_port"])
            self.cat_relays[name] = relay
            self._cat_tasks[name] = asyncio.create_task(relay.run())

        if changed:
            self._save_config()

    def _save_config(self):
        if self.config_path:
            self.config_path.write_text(json.dumps(self.app_cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    def radio_status(self, name: str):
        client = self.status_clients.get(name)
        return client.busy_by if client else None

    async def switch_to(self, radio_cfg: dict):
        await self._teardown()
        self.radio_name = radio_cfg["name"]

        self.control = ControlClient(self.username, self.server_host, radio_cfg["control_port"])
        self._tasks.append(asyncio.create_task(self.control.run()))

        audio_cfg = self.app_cfg["audio"]
        self.audio = AudioLink(
            input_device=audio_cfg.get("input_device"),
            output_device=audio_cfg.get("output_device"),
            listen_port=audio_cfg["local_port"],
            peer=(self.server_host, radio_cfg["audio_port"]),
            mic_gain=audio_cfg.get("mic_gain", 1.0),
            speaker_gain=audio_cfg.get("speaker_gain", 1.0),
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
        relay = self.cat_relays.get(self.radio_name)
        if rc28_cfg.get("enabled") and radio_cfg.get("civ_address") and relay:
            self.rc28 = Rc28Driver(relay.send_cat, radio_cfg["civ_address"], rc28_cfg.get("step_hz", 10))
            relay.on_cat_data = self.rc28.on_cat_reply
            self._tasks.append(asyncio.create_task(self.rc28.run()))
        else:
            self.rc28 = None

        log.info("switched to radio %s", self.radio_name)

    async def _teardown(self):
        for relay in self.cat_relays.values():
            relay.on_cat_data = None  # clear any stale RC-28 hookup from the previously-selected radio
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

    async def reconnect(self, new_server_host: str):
        """Switches every connection (status watchers, per-radio CAT
        relays, and the active radio, if any) over to a different server
        address."""
        self.server_host = new_server_host
        self.app_cfg["server_host"] = new_server_host

        for task in self._cat_tasks.values():
            task.cancel()
        self._cat_tasks = {}
        self.cat_relays = {}

        radios = await self.refresh_radios()

        radio_name = self.radio_name
        if radio_name:
            radio_cfg = next((r for r in radios if r["name"] == radio_name), None)
            if radio_cfg:
                await self.switch_to(radio_cfg)

    async def shutdown(self):
        await self._teardown()
        for task in self._cat_tasks.values():
            task.cancel()
        for client in self.status_clients.values():
            if client.writer:
                client.writer.close()


if __name__ == "__main__":
    class _FakeTask:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    async def _demo():
        # Radio "B" goes inactive: its CAT relay/task must be pruned.
        # Radio "A" stays active and already has a relay: start_cat_relays
        # must leave it alone rather than recreating it (no com0com/network
        # touched in this check).
        session = RadioSession({"username": "t", "server_host": "127.0.0.1", "com": {"baud": 19200}})
        task_b = _FakeTask()
        session.cat_relays = {"A": object(), "B": object()}
        session._cat_tasks = {"A": _FakeTask(), "B": task_b}
        await session.start_cat_relays([
            {"name": "A", "active": True, "cat_port": 1},
            {"name": "B", "active": False, "cat_port": 2},
        ])
        assert "B" not in session.cat_relays
        assert "B" not in session._cat_tasks
        assert task_b.cancelled
        assert "A" in session.cat_relays

    asyncio.run(_demo())
    print("session.py: ok")
