"""Bridges a local virtual COM port (com0com) to the server's CAT TCP bridge.

The other end of the com0com pair is what real CAT software (WSJT-X,
N1MM+, fldigi) opens. PTT does NOT go through here — see control.py —
because arbitration has to see it before it reaches the radio.
"""

import asyncio
import logging

import serial

log = logging.getLogger("com_relay")


class ComRelay:
    def __init__(self, com_port: str, baud: int, server_host: str, server_port: int):
        self.com_port = com_port
        self.baud = baud
        self.server_host = server_host
        self.server_port = server_port
        self.writer = None
        self.serial = None

    async def run(self):
        self.serial = serial.Serial(self.com_port, self.baud, timeout=0)
        reader, writer = await asyncio.open_connection(self.server_host, self.server_port)
        self.writer = writer
        log.info("connected to CAT bridge %s:%s <-> %s", self.server_host, self.server_port, self.com_port)
        try:
            await asyncio.gather(self._pump_serial_to_tcp(), self._pump_tcp_to_serial(reader))
        finally:
            self.writer = None

    async def _pump_serial_to_tcp(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.run_in_executor(None, self.serial.read, 256)
            if data:
                self.writer.write(data)
                await self.writer.drain()
            else:
                await asyncio.sleep(0.01)

    async def _pump_tcp_to_serial(self, reader):
        while True:
            data = await reader.read(256)
            if not data:
                log.warning("CAT bridge connection closed")
                break
            self.serial.write(data)
