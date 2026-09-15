"""External WinKeyer-style hardware keyer passthrough. The WinKeyer does
its own dit/dah timing (from its own paddle input or from text sent to
it); we only need to know when it's actually keying, to relay that as
our CW event stream.

ponytail / needs real hardware to verify: HOST_OPEN and the "high bit set
= status byte, bit 0x04 = keying" framing follow the published K1EL
WinKeyer protocol, but this has not been exercised against a real
WinKeyer here — confirm before relying on it, and expect to adjust
KEYDOWN_BIT if your firmware version reports differently.
"""

import asyncio
import logging

import serial_asyncio

log = logging.getLogger("winkeyer")

HOST_OPEN = bytes([0x00, 0x02])
HOST_CLOSE = bytes([0x00, 0x03])
KEYDOWN_BIT = 0x04


class WinkeyerSource:
    def __init__(self, port: str, baud: int, on_key):
        self.port = port
        self.baud = baud
        self.on_key = on_key
        self._transport = None

    async def run(self):
        loop = asyncio.get_running_loop()
        source = self

        class _Proto(asyncio.Protocol):
            def connection_made(self, transport):
                source._transport = transport
                transport.write(HOST_OPEN)

            def data_received(self, data):
                for byte in data:
                    if byte & 0x80:  # status bytes have the high bit set
                        source.on_key(bool(byte & KEYDOWN_BIT))

            def connection_lost(self, exc):
                log.warning("WinKeyer connection lost: %s", exc)

        self._transport, _ = await serial_asyncio.create_serial_connection(
            loop, _Proto, self.port, baudrate=self.baud
        )

    def stop(self):
        if self._transport:
            self._transport.write(HOST_CLOSE)
            self._transport.close()
