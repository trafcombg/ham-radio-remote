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
import base64
import contextlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import serial

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
AMPLIFIER_POLL_INTERVAL_S = 5
CONTROL_RECONNECT_DELAY_S = 3.0
CAT_BUSY_POLL_INTERVAL_S = 4.0


def _is_port_free(port: str) -> bool:
    """Windows COM ports are exclusive-access by default — if some other
    process (WSJT-X, N1MM+, ...) already has the exposed com0com port
    open, trying to open it ourselves fails. That's the whole check."""
    try:
        serial.Serial(port, timeout=0).close()
        return True
    except (serial.SerialException, OSError):
        return False


async def _run_control_client_forever(client: ControlClient):
    """ControlClient.run() returns (cleanly or via exception) the instant
    the TCP connection drops for any reason — a server restart, a Wi-Fi
    blip, anything — and nothing used to retry it. That silently killed
    PTT/status for that radio until the operator manually switched away
    and back: a "hold PTT, it transmits, release does nothing" report
    turned out to be exactly this — the release never reached the server
    because the connection was already gone by then. Keeps retrying until
    the server explicitly denies our credentials/permission (denied=True),
    which would just be hammered pointlessly otherwise."""
    while True:
        try:
            await client.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("control connection to %s:%s lost — reconnecting in %.0fs", client.server_host, client.server_port, CONTROL_RECONNECT_DELAY_S, exc_info=True)
        if client.denied:
            return
        await asyncio.sleep(CONTROL_RECONNECT_DELAY_S)


async def _run_cat_relay_forever(relay: ComRelay):
    """Same gap as _run_control_client_forever, but for the per-radio CAT
    TCP relay — a dropped server connection here (e.g. a server restart)
    used to leave relay.run() dead with nothing retrying it, so the radio
    stayed stuck on "Няма връзка" until the operator manually switched
    away from and back to that radio."""
    while True:
        try:
            await relay.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning(
                "CAT връзка към %s:%s изгубена — нов опит след %.0fs",
                relay.server_host, relay.server_port, CONTROL_RECONNECT_DELAY_S, exc_info=True,
            )
        await asyncio.sleep(CONTROL_RECONNECT_DELAY_S)


class RadioSession:
    def __init__(self, app_cfg: dict, config_path=None):
        self.app_cfg = app_cfg
        self.config_path = config_path
        self.username = app_cfg["username"]
        self.password = app_cfg.get("password", "")
        self.server_host = app_cfg["server_host"]

        self.status_clients: dict = {}  # radio name -> ControlClient, for the radio picker
        self._status_tasks: dict = {}
        self.cat_relays: dict = {}      # radio name -> ComRelay, one per ACTIVE radio, always on
        self._cat_tasks: dict = {}
        self.external_cat_busy: dict = {}  # radio name -> bool, exposed COM port held by another app — see start_cat_busy_poll()
        self._cat_busy_poll_task = None

        self.server_version = None
        self.amplifiers: list = []  # polled periodically — see start_amplifier_poll(); ui.py just reads this
        self._amp_poll_task = None
        self.radio_name = None
        self.control = None
        self.audio = None
        self.audio_error = None  # one-shot — set on AudioLink setup failure, ui.py shows+clears it
        self.cw_link = None
        self.text_cw = None
        self.iambic_keyer = None
        self.paddle = None
        self.winkeyer = None
        self.rc28 = None
        self._tasks = []

    def _api_base(self) -> str:
        return f"http://{self.server_host}:{self.app_cfg.get('server_api_port', 8080)}"

    def _basic_auth_header(self) -> str:
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        return f"Basic {token}"

    def _fetch_radios(self) -> list:
        url = f"{self._api_base()}/api/client/radios"
        with urllib.request.urlopen(url, timeout=RADIOS_FETCH_TIMEOUT_S) as resp:
            body = json.loads(resp.read())
        self.server_version = body.get("server_version")
        return body["radios"]

    def _fetch_amplifiers_sync(self) -> list:
        req = urllib.request.Request(
            f"{self._api_base()}/api/client/amplifiers", headers={"Authorization": self._basic_auth_header()}
        )
        with urllib.request.urlopen(req, timeout=RADIOS_FETCH_TIMEOUT_S) as resp:
            return json.loads(resp.read())["amplifiers"]

    async def fetch_amplifiers(self) -> list:
        """Every configured amplifier, each flagged with can_control for
        this user — the caller (UI) greys out ones this user can't touch
        rather than hiding them, same as inactive radios."""
        try:
            return await asyncio.to_thread(self._fetch_amplifiers_sync)
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("не успях да взема списъка с усилватели (%s)", e)
            return []

    def _set_amplifier_mode_sync(self, name: str, mode: str) -> tuple:
        url = f"{self._api_base()}/api/client/amplifiers/{urllib.parse.quote(name)}/mode"
        req = urllib.request.Request(
            url, method="POST",
            data=json.dumps({"mode": mode}).encode(),
            headers={"Authorization": self._basic_auth_header(), "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=RADIOS_FETCH_TIMEOUT_S) as resp:
                return True, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            return False, detail

    async def set_amplifier_mode(self, name: str, mode: str) -> tuple:
        return await asyncio.to_thread(self._set_amplifier_mode_sync, name, mode)

    async def start_amplifier_poll(self):
        if self._amp_poll_task is None:
            self._amp_poll_task = asyncio.create_task(self._amplifier_poll_loop())

    async def _amplifier_poll_loop(self):
        while True:
            self.amplifiers = await self.fetch_amplifiers()
            await asyncio.sleep(AMPLIFIER_POLL_INTERVAL_S)

    async def start_cat_busy_poll(self):
        if self._cat_busy_poll_task is None:
            self._cat_busy_poll_task = asyncio.create_task(self._cat_busy_poll_loop())

    async def _cat_busy_poll_loop(self):
        """Radio picker only ever showed PTT-holder busyness — a radio
        whose exposed COM port a local CAT app (WSJT-X etc.) already has
        open looked "free" even though opening it again from elsewhere
        would just fail. Surface that too."""
        while True:
            com_ports = self.app_cfg.get("com_ports", {})
            busy = {}
            for name in self.cat_relays:
                exposed = com_ports.get(name, {}).get("local")
                if exposed:
                    busy[name] = not await asyncio.to_thread(_is_port_free, exposed)
            self.external_cat_busy = busy
            await asyncio.sleep(CAT_BUSY_POLL_INTERVAL_S)

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
        for task in self._status_tasks.values():
            task.cancel()
        self._status_tasks = {}
        for client in self.status_clients.values():
            if client.writer:
                client.writer.close()
        self.status_clients = {}
        for radio_cfg in self.app_cfg.get("radios", []):
            if not radio_cfg.get("active", True):
                continue
            client = ControlClient(self.username, self.password, self.server_host, radio_cfg["control_port"])
            self.status_clients[radio_cfg["name"]] = client
            self._status_tasks[radio_cfg["name"]] = asyncio.create_task(_run_control_client_forever(client))

    async def start_cat_relays(self, radios: list):
        active_names = {r["name"] for r in radios if r.get("active", True)}
        for name in list(self.cat_relays):
            if name not in active_names:
                self._cat_tasks.pop(name).cancel()
                del self.cat_relays[name]

        try:
            setupc = await asyncio.to_thread(com0com.find_setupc)
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
                    exposed, internal = await asyncio.to_thread(com0com.create_pair, setupc)
                except Exception:
                    log.exception("не успях да създам виртуален COM порт за %s", name)
                    continue
                mapping = {"local": exposed, "internal": internal}
                com_ports[name] = mapping
                changed = True
            relay = ComRelay(mapping["internal"], self.app_cfg["com"].get("baud", 19200), self.server_host, radio["cat_port"])
            self.cat_relays[name] = relay
            self._cat_tasks[name] = asyncio.create_task(_run_cat_relay_forever(relay))

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

        self.control = ControlClient(self.username, self.password, self.server_host, radio_cfg["control_port"])
        self.control.on_reconfigured = lambda name=radio_cfg["name"]: asyncio.create_task(self._handle_reconfigured(name))
        self._tasks.append(asyncio.create_task(_run_control_client_forever(self.control)))

        audio_cfg = self.app_cfg["audio"]
        # Per-radio device override (Settings) falls back to the global
        # default — lets each radio route to a different physical
        # mic/speaker (e.g. two radios, two USB sound cards).
        radio_audio = self.app_cfg.get("radio_audio", {}).get(radio_cfg["name"], {})
        input_device = radio_audio.get("input_device")
        if input_device is None:
            input_device = audio_cfg.get("input_device")
        output_device = radio_audio.get("output_device")
        if output_device is None:
            output_device = audio_cfg.get("output_device")
        try:
            self.audio = AudioLink(
                input_device=input_device,
                output_device=output_device,
                listen_port=audio_cfg["local_port"],
                peer=(self.server_host, radio_cfg["audio_port"]),
                mic_gain=audio_cfg.get("mic_gain", 1.0),
                speaker_gain=audio_cfg.get("speaker_gain", 1.0),
                latency=audio_cfg.get("latency", "low"),
                codec=radio_cfg.get("codec", "pcm16"),
                sample_rate=radio_cfg.get("sample_rate", 48000),
            )
            self.audio.start()
        except Exception as e:
            # switch_to() runs via run_coroutine_threadsafe with nobody
            # awaiting the future — an uncaught exception here used to
            # vanish silently (CAT/control still connected, so the UI
            # looked fine while audio just never worked). Surface it
            # instead of raising, so the rest of the radio still works —
            # ui.py's _tick() polls this and shows it once.
            log.exception("audio setup failed for radio %s", self.radio_name)
            self.audio = None
            self.audio_error = f"Аудио неуспешно: {e}"

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

    async def _handle_reconfigured(self, radio_name: str):
        """The control connection just told us the admin changed this
        radio's config server-side (e.g. codec/sample_rate) and closed on
        us. switch_to() built the AudioLink from whatever settings were
        current back then, and nothing ever rebuilt it — so the two sides
        could silently drift onto different codecs/rates, which sounds
        like garbled/choppy audio, not a dropped connection. Re-fetch and
        re-switch to pick up whatever changed, but only if the operator
        hasn't already switched to a different radio in the meantime."""
        if radio_name != self.radio_name:
            return
        radios = await self.refresh_radios()
        radio_cfg = next((r for r in radios if r["name"] == radio_name), None)
        if radio_cfg and radio_cfg.get("active", True) and radio_name == self.radio_name:
            await self.switch_to(radio_cfg)

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
        if self._amp_poll_task:
            self._amp_poll_task.cancel()
        if self._cat_busy_poll_task:
            self._cat_busy_poll_task.cancel()
        for task in self._cat_tasks.values():
            task.cancel()
        for task in self._status_tasks.values():
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

    async def _demo_reconnect():
        # Reproduces the reported bug: the control connection drops (any
        # reason — server restart, network blip) and PTT/status for that
        # radio silently died forever because nothing reconnected it.
        global CONTROL_RECONNECT_DELAY_S
        CONTROL_RECONNECT_DELAY_S = 0.15  # self-check only — production stays 3s

        hellos = []

        async def handle(reader, writer):
            line = await reader.readline()
            hellos.append(json.loads(line))
            await asyncio.sleep(0.05)
            writer.close()  # simulate the connection dying right after hello

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            client = ControlClient("ivan", "secret", "127.0.0.1", port)
            task = asyncio.create_task(_run_control_client_forever(client))
            await asyncio.sleep(0.4)  # long enough for at least 2 connect-drop-retry cycles
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert len(hellos) >= 2, f"expected the client to reconnect after the drop, got {len(hellos)} hello(s)"

    assert _is_port_free("COM_DOES_NOT_EXIST_9999") is False  # can't open -> reported busy, not a crash

    async def _demo_reconfigured_ignores_stale_radio():
        # The operator already switched to a different radio by the time
        # a late "reconfigured" arrives for the OLD one — must not yank
        # them back onto it (and must not touch the network to find out).
        session = RadioSession({"username": "t", "server_host": "127.0.0.1", "com": {"baud": 19200}})
        session.radio_name = "CURRENT"
        await session._handle_reconfigured("STALE")
        assert session.radio_name == "CURRENT"

    asyncio.run(_demo())
    asyncio.run(_demo_reconnect())
    asyncio.run(_demo_reconfigured_ignores_stale_radio())
    print("session.py: ok")
