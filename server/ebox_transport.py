"""Transports for reaching an ACOM amplifier.

SerialAcomTransport and RawTcpAcomTransport both speak the CONFIRMED
protocol in common/acom_protocol.py — the only difference is the wire
(a local COM port vs. a TCP socket to the eBox's IP). RawTcpAcomTransport
is an educated guess: many Ethernet-serial bridges expose the raw byte
stream transparently on a TCP port, which would make it work unmodified,
but this has NOT been confirmed against a real eBox.

HttpEboxTransport speaks eBox's own web protocol (reverse-engineered
live against a real ACOM 1200S/eBox — see its own docstring for exactly
what's confirmed and what's still missing).
"""

import asyncio
import logging
import re
import urllib.parse
import urllib.request

import serial_asyncio

from common.acom_protocol import CMD_OFF, CMD_OPERATE, CMD_STANDBY

log = logging.getLogger("ebox_transport")


class _RelayProtocol(asyncio.Protocol):
    def __init__(self, on_data):
        self.transport = None
        self.on_data = on_data

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        self.on_data(data)

    def connection_lost(self, exc):
        log.warning("amplifier connection lost: %s", exc)


class SerialAcomTransport:
    """Direct RS-232 to the amplifier — the same connection
    bjornekelund/ACOM-Controller uses. No eBox involved; useful if the
    amp is plugged straight into the server, or into a machine reachable
    some other way."""

    def __init__(self, port: str):
        self.port = port
        self._proto = None

    async def connect(self, on_data):
        loop = asyncio.get_running_loop()
        _, self._proto = await serial_asyncio.create_serial_connection(
            loop, lambda: _RelayProtocol(on_data), self.port, baudrate=9600,
        )
        conn = self._proto.transport.serial
        conn.dtr = False  # the amp uses DTR/RTS for its own front-panel power button
        conn.rts = False

    def write(self, data: bytes):
        if self._proto and self._proto.transport:
            self._proto.transport.write(data)

    def close(self):
        if self._proto and self._proto.transport:
            self._proto.transport.close()


class RawTcpAcomTransport:
    """The same confirmed protocol, over a raw TCP socket to the eBox's
    IP — try this first against real hardware. UNCONFIRMED against an
    actual eBox, but if it works, everything above this transport layer
    (amplifier_bridge.py, the admin panel, DB logging) just works too."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self._writer = None
        self._task = None

    async def connect(self, on_data):
        reader, writer = await asyncio.open_connection(self.host, self.port)
        self._writer = writer
        self._task = asyncio.create_task(self._pump(reader, on_data))

    async def _pump(self, reader, on_data):
        while True:
            data = await reader.read(256)
            if not data:
                log.warning("amplifier TCP connection closed")
                break
            on_data(data)

    def write(self, data: bytes):
        if self._writer:
            self._writer.write(data)

    def close(self):
        if self._writer:
            self._writer.close()
        if self._task:
            self._task.cancel()


_SSI_FIELD_RE = re.compile(r'"(\w+)":\{[^{}]*?\bv:(-?\d+(?:\.\d+)?)')


def _parse_ssi(body: str) -> dict:
    """Parses one eBox .ssi JSONP response body (e.g. '_4({"sig01":{id:20,
    v:0,f:2},...});') into a flat {field_name: number} dict. Not valid
    JSON — keys are unquoted JS identifiers (id:20 not "id":20) — so this
    pulls each top-level field's v: out with a regex instead of a real
    JS-object parser, which is overkill for the flat sigNN/measNN shape
    every .ssi response actually uses."""
    return {
        name: (float(v) if "." in v else int(v))
        for name, v in _SSI_FIELD_RE.findall(body)
    }


class HttpEboxTransport:
    """eBox web control — reverse-engineered live against a real ACOM
    1200S/eBox (callsign LZ4TL, 2026-09) by reading its own JS
    (js/home.js, js/dvf.core.js) and capturing its requests. CONFIRMED:

      - login:   POST /login.shtml   body user=<user>&pass=<pass>
      - logout:  GET  /logout.shtml
      - control: POST /ctrl.shtml    body {action}={value}, e.g.
                 mode=1 -> Operate, mode=0 -> Standby
                 pwr=1  -> Power ON, pwr=0  -> Power OFF
      - telemetry: JSONP polling GET /read.ssi?idx=<idx> (see _parse_ssi)

    Session tracking is server-side (by client, not a cookie the browser
    ever receives) and the device only allows ONE logged-in session at a
    time — a second login attempt gets "Another session underway", not a
    new session replacing the old one.

    Telemetry decoding is intentionally partial: only temperature
    (sig03 — confirmed by exact value match against the amp's own
    display) and the four status LEDs (sig11-14: Overheat/CAT/TX
    Disabled/Transmit — confirmed via status.ssi's own leds list) are
    decoded. Forward/reflected power, SWR, and DC current/voltage were
    NOT confirmed: the amp was idle (0 W) during capture, so every
    zero-valued sig/meas field is an untested guess and every other
    field in AmplifierBridge's telemetry dict is left None rather than
    risk feeding a wrong value into `fault`, which blocks radio PTT.

    ponytail: power/SWR/DC fields left undecoded (None) — key the amp
    into Operate on a dummy load to get nonzero samples and finish the
    sig/meas -> field mapping in _parse_telemetry below.
    """

    POLL_INTERVAL_S = 1.0
    _ACTION_BY_CMD = {CMD_OPERATE: ("mode", "1"), CMD_STANDBY: ("mode", "0"), CMD_OFF: ("pwr", "0")}

    def __init__(self, base_url: str, username: str | None = None, password: str | None = None, idx: int = 5):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.idx = idx  # widget/session slot the device's own JS always used; observed constant, meaning unconfirmed
        self._poll_task = None
        self._closed = False

    async def connect(self, on_data):
        if self.username:
            await self._post("/login.shtml", {"user": self.username, "pass": self.password or ""})
        self._poll_task = asyncio.create_task(self._poll_loop(on_data))

    async def _poll_loop(self, on_data):
        while not self._closed:
            try:
                fields = _parse_ssi(await self._get(f"/read.ssi?idx={self.idx}"))
                on_data(self._parse_telemetry(fields))
            except Exception:
                log.warning("eBox telemetry poll failed", exc_info=True)
            await asyncio.sleep(self.POLL_INTERVAL_S)

    @staticmethod
    def _parse_telemetry(fields: dict) -> dict:
        return {
            "status": None,
            "temp_c": fields.get("sig03"),
            "fan": None,
            "band": None,
            "drive_power_w": None,
            "output_power_w": None,
            "reflected_power_w": None,
            "swr": None,
            "dc_power_w": None,
            "error_code": None,
            "fault": bool(fields.get("sig11", 0)),
        }

    def write(self, data: bytes):
        action = self._ACTION_BY_CMD.get(bytes(data))
        if action is None:
            return  # CMD_ENABLE/DISABLE_TELEMETRY and raw CAT mirror bytes have no eBox equivalent — polling covers telemetry regardless
        name, value = action
        asyncio.create_task(self._post("/ctrl.shtml", {name: value}))

    def close(self):
        self._closed = True
        if self._poll_task:
            self._poll_task.cancel()
        if self.username:
            asyncio.create_task(self._get("/logout.shtml"))

    async def _get(self, path: str) -> str:
        return await asyncio.get_running_loop().run_in_executor(None, self._get_sync, path)

    def _get_sync(self, path: str) -> str:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as resp:
            return resp.read().decode("utf-8", errors="replace")

    async def _post(self, path: str, fields: dict):
        await asyncio.get_running_loop().run_in_executor(None, self._post_sync, path, fields)

    def _post_sync(self, path: str, fields: dict):
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=urllib.parse.urlencode(fields).encode(), method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()


if __name__ == "__main__":
    assert _parse_ssi('_4({"sig01":{id:20,v:0,f:2},"sig03":{id:22,v:29,f:2},"idx":5});') == {
        "sig01": 0, "sig03": 29,
    }
    assert _parse_ssi('_1({"adm":0,"ses":5995,"alias":"LZ4TL","idx":5});') == {}

    t = HttpEboxTransport("http://127.0.0.1")
    assert t._parse_telemetry({"sig03": 29, "sig11": 0}) == {
        "status": None, "temp_c": 29, "fan": None, "band": None, "drive_power_w": None,
        "output_power_w": None, "reflected_power_w": None, "swr": None, "dc_power_w": None,
        "error_code": None, "fault": False,
    }
    assert t._parse_telemetry({"sig11": 1})["fault"] is True
    assert t._ACTION_BY_CMD[CMD_OPERATE] == ("mode", "1")
    assert t._ACTION_BY_CMD[CMD_STANDBY] == ("mode", "0")
    assert t._ACTION_BY_CMD[CMD_OFF] == ("pwr", "0")

    print("ebox_transport.py: ok")
