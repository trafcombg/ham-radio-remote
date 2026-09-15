"""Bridges a local virtual COM port (com0com) to the server's CAT TCP bridge.

The other end of the com0com pair is what real CAT software (WSJT-X,
N1MM+, fldigi) opens. PTT does NOT go through here — see control.py —
because arbitration has to see it before it reaches the radio. Plain CAT
traffic (e.g. RC-28's frequency-change commands, see client/rc28.py) is
not arbitrated — it's the same kind of command any CAT app already sends
through this tunnel — so send_cat()/on_cat_data write/observe it directly.
"""

import asyncio
import contextlib
import logging

import serial

log = logging.getLogger("com_relay")

# No app-level keepalive is possible on this channel — it's a raw CAT byte
# passthrough (server forwards every byte straight to the real radio), so
# injecting synthetic pings would corrupt the CI-V stream. This just has to
# be generous enough not to fire during a legitimate quiet stretch.
IDLE_TIMEOUT_S = 60.0


class ComRelay:
    def __init__(self, com_port: str, baud: int, server_host: str, server_port: int):
        self.com_port = com_port
        self.baud = baud
        self.server_host = server_host
        self.server_port = server_port
        self.writer = None
        self.serial = None
        self.on_cat_data = None  # optional callable(bytes) — e.g. RC-28 watching for frequency replies

    async def run(self):
        self.serial = serial.Serial(self.com_port, self.baud, timeout=0)
        reader, writer = await asyncio.open_connection(self.server_host, self.server_port)
        self.writer = writer
        log.info("connected to CAT bridge %s:%s <-> %s", self.server_host, self.server_port, self.com_port)
        try:
            # gather() would wait for BOTH forever — _pump_serial_to_tcp
            # never returns on its own, so a dead TCP side (the other
            # coroutine finishing/raising) used to leave run() hung
            # instead of letting the caller's reconnect loop take over.
            tasks = [asyncio.create_task(self._pump_serial_to_tcp()), asyncio.create_task(self._pump_tcp_to_serial(reader))]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for task in done:
                task.result()  # re-raise, e.g. a serial read error
        finally:
            self.writer = None
            writer.close()
            if self.serial:
                self.serial.close()
                self.serial = None

    async def _pump_serial_to_tcp(self):
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.run_in_executor(None, self.serial.read, 256)
            if data:
                log.debug("%s -> server: %s", self.com_port, data.hex())
                self.writer.write(data)
                await self.writer.drain()
            else:
                await asyncio.sleep(0.01)

    async def _pump_tcp_to_serial(self, reader):
        while True:
            try:
                data = await asyncio.wait_for(reader.read(256), timeout=IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("no CAT traffic for %.0fs — assuming the connection is dead", IDLE_TIMEOUT_S)
                break
            if not data:
                log.warning("CAT bridge connection closed")
                break
            log.debug("server -> %s: %s", self.com_port, data.hex())
            self.serial.write(data)
            if self.on_cat_data:
                self.on_cat_data(data)

    async def send_cat(self, data: bytes):
        if self.writer:
            self.writer.write(data)
            await self.writer.drain()


if __name__ == "__main__":
    import unittest.mock as mock

    async def _demo_idle_timeout():
        # A CAT bridge connection that goes silent (no data, no close) must
        # be treated as dead after IDLE_TIMEOUT_S, not hang forever — this
        # is what let a client stay stuck on "Няма връзка" after a server
        # restart if the TCP close wasn't clean.
        global IDLE_TIMEOUT_S
        IDLE_TIMEOUT_S = 0.05  # self-check only — production stays 60s

        async def handle(reader, writer):
            await asyncio.sleep(10)  # never sends anything, never closes

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            relay = ComRelay("COM_FAKE", 19200, "127.0.0.1", port)
            relay.serial = mock.Mock()
            start = asyncio.get_event_loop().time()
            await relay._pump_tcp_to_serial(reader)  # must return once idle timeout fires
            assert asyncio.get_event_loop().time() - start < 1.0
            writer.close()

    asyncio.run(_demo_idle_timeout())
    print("com_relay.py: ok")
