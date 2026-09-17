"""Serial transport for the RSW8A1ER antenna switch — a plain RS-232
device, unlike the ACOM amplifier there's no known network-attached
variant (no eBox-style Ethernet bridge), so this is the only transport.

ponytail: the sniffer capture that reverse-engineered the protocol
showed the vendor's own control software raising DTR ~21s before its
first command, but the switch replied within 22ms of that — far too
fast for a boot/reset-on-DTR device, so DTR/RTS are left untouched here
rather than force-cleared like the CI-V/PTT-related serial ports
elsewhere in this project. Revisit if a real unit ever fails to respond
until DTR is toggled.
"""

import asyncio
import logging

import serial_asyncio

log = logging.getLogger("antenna_switch_transport")

DEFAULT_BAUD = 9600  # matches the captured session, but not every unit uses this — configurable per switch


class _RelayProtocol(asyncio.Protocol):
    def __init__(self, on_data):
        self.transport = None
        self.on_data = on_data

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        self.on_data(data)

    def connection_lost(self, exc):
        log.warning("antenna switch connection lost: %s", exc)


class SerialAntennaSwitchTransport:
    def __init__(self, port: str, baud: int = DEFAULT_BAUD):
        self.port = port
        self.baud = baud
        self._proto = None

    async def connect(self, on_data):
        loop = asyncio.get_running_loop()
        _, self._proto = await serial_asyncio.create_serial_connection(
            loop, lambda: _RelayProtocol(on_data), self.port, baudrate=self.baud,
        )

    def write(self, data: bytes):
        if self._proto and self._proto.transport:
            self._proto.transport.write(data)

    def close(self):
        if self._proto and self._proto.transport:
            self._proto.transport.close()
