"""Transports for reaching an ACOM amplifier.

SerialAcomTransport and RawTcpAcomTransport both speak the CONFIRMED
protocol in common/acom_protocol.py — the only difference is the wire
(a local COM port vs. a TCP socket to the eBox's IP). RawTcpAcomTransport
is an educated guess: many Ethernet-serial bridges expose the raw byte
stream transparently on a TCP port, which would make it work unmodified,
but this has NOT been confirmed against a real eBox.

HttpEboxTransport is the one that needs real work: eBox's actual web
protocol isn't publicly documented, and there's no device or browser
capture available here to reverse it (see its own docstring below for
exactly what's needed).
"""

import asyncio
import logging

import serial_asyncio

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


class HttpEboxTransport:
    """UNIMPLEMENTED. eBox's actual HTTP protocol is not publicly
    documented by ACOM, and this environment has neither a physical eBox
    nor a browser session to capture one from.

    To make this real, per the project plan: open the eBox's web UI in
    Chrome, open DevTools -> Network, operate it (standby/operate,
    power on/off) and watch the requests it makes. Two outcomes:

    1. It's a clean JSON/REST API behind the page -> call it directly
       with an HTTP client (httpx/aiohttp) in connect()/write() below.
    2. It's just HTML forms -> connect() should GET the status page and
       scrape the values (BeautifulSoup or plain regex) into the same
       dict shape common.acom_protocol.decode_telemetry() returns, and
       write() should POST the same form fields the buttons submit.
       Note in the code that this is sensitive to eBox firmware updates
       changing the page markup (the project plan calls this out too).

    Try RawTcpAcomTransport first — it needs zero reverse engineering
    and may just work.
    """

    def __init__(self, base_url: str, username: str | None = None, password: str | None = None):
        self.base_url = base_url
        self.username = username
        self.password = password

    async def connect(self, on_data):
        raise NotImplementedError(
            "HttpEboxTransport needs eBox's real HTTP protocol, captured from a live device "
            "(Chrome DevTools -> Network tab) — see this class's docstring. Try "
            "RawTcpAcomTransport first."
        )

    def write(self, data: bytes):
        raise NotImplementedError

    def close(self):
        pass
