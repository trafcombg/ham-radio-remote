"""Bridges a local virtual COM port (com0com) to the server's CAT TCP bridge.

The other end of the com0com pair is what real CAT software (WSJT-X,
N1MM+, fldigi) opens. PTT does NOT go through here — see control.py —
because arbitration has to see it before it reaches the radio. Plain CAT
traffic (e.g. RC-28's frequency-change commands, see client/rc28.py) is
not arbitrated — it's the same kind of command any CAT app already sends
through this tunnel — so send_cat()/cat_listeners write/observe it
directly.
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
        self.bytes_sent = 0     # cumulative — status_panel.py derives a speed from the deltas
        self.bytes_recv = 0
        # Multiple independent consumers watch CAT replies at once now —
        # RC-28 (frequency deltas) and the built-in radio panel
        # (frequency/mode/S-meter) can both be active on the same radio.
        self.cat_listeners: list = []
        self.on_line_state_change = None  # optional callable(cts, dsr) — external RTS/DTR PTT, see session.py

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
        # ponytail: assumes com0com's default pair setup cross-wires
        # RTS<->CTS and DTR<->DSR between the two halves, same as a real
        # null-modem cable — not verified against an actual installed
        # driver. If a real setup doesn't cross-wire this, external
        # RTS/DTR PTT detection just never fires; CI-V PTT (see
        # radio_bridge._on_external_cat_write) is unaffected either way.
        loop = asyncio.get_running_loop()
        last_lines = None
        while True:
            data = await loop.run_in_executor(None, self.serial.read, 256)
            if data:
                log.debug("%s -> server: %s", self.com_port, data.hex())
                self.writer.write(data)
                self.bytes_sent += len(data)
                await self.writer.drain()
            else:
                await asyncio.sleep(0.01)
            lines = (self.serial.cts, self.serial.dsr)
            if lines != last_lines:
                last_lines = lines
                if self.on_line_state_change:
                    self.on_line_state_change(*lines)

    async def _pump_tcp_to_serial(self, reader):
        loop = asyncio.get_running_loop()
        while True:
            try:
                data = await asyncio.wait_for(reader.read(256), timeout=IDLE_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("no CAT traffic for %.0fs — assuming the connection is dead", IDLE_TIMEOUT_S)
                break
            if not data:
                log.warning("CAT bridge connection closed")
                break
            self.bytes_recv += len(data)
            log.debug("server -> %s: %s", self.com_port, data.hex())
            # Our own listeners (built-in radio panel, RC-28) get the data
            # right away — they must not wait on the com0com write below.
            for listener in self.cat_listeners:
                listener(data)
            # self.serial.write() is a blocking Win32 WriteFile() call —
            # when nothing reads the OTHER end of the com0com pair (no
            # third-party CAT app connected to the exposed port), the
            # buffer fills and this never returns. Run synchronously on
            # the loop thread (as it used to be), it freezes the WHOLE
            # event loop forever, including every unrelated task on it —
            # which is exactly why the built-in radio panel showed no
            # reaction even though replies were arriving fine.
            await loop.run_in_executor(None, self.serial.write, data)

    async def send_cat(self, data: bytes):
        # Unlike _pump_serial_to_tcp (com0com -> server, third-party CAT
        # apps like RS-BA1), this is OUR OWN outgoing path (RadioPanelController,
        # RC-28) and previously had no logging at all — "nothing in the
        # debug log" was indistinguishable from "nothing being sent".
        if self.writer:
            log.debug("send_cat -> server: %s", data.hex())
            self.writer.write(data)
            self.bytes_sent += len(data)
            await self.writer.drain()
        else:
            log.debug("send_cat dropped (no server connection yet): %s", data.hex())

    def add_cat_listener(self, callback):
        self.cat_listeners.append(callback)

    def remove_cat_listener(self, callback):
        if callback in self.cat_listeners:
            self.cat_listeners.remove(callback)

    def clear_cat_listeners(self):
        self.cat_listeners = []


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

    async def _demo_line_state_change():
        # A third-party CAT app toggles RTS on the exposed com0com port —
        # this (internal) port sees it as a CTS change and must report it
        # via on_line_state_change, independent of any CAT data flowing.
        class _FakeSerial:
            def __init__(self):
                self.cts = False
                self.dsr = False

            def read(self, n):
                return b""

        relay = ComRelay("COM_FAKE", 19200, "127.0.0.1", 0)
        relay.serial = _FakeSerial()
        relay.writer = mock.Mock()
        changes = []
        relay.on_line_state_change = lambda cts, dsr: changes.append((cts, dsr))

        task = asyncio.create_task(relay._pump_serial_to_tcp())
        await asyncio.sleep(0.03)
        relay.serial.cts = True
        await asyncio.sleep(0.03)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert (False, False) in changes, f"initial line state never reported: {changes}"
        assert (True, False) in changes, f"CTS change never reported: {changes}"

    async def _demo_multiple_cat_listeners():
        # RC-28 and the built-in radio panel both watch CAT replies on
        # the same relay at once — both must see every frame, and
        # clear_cat_listeners() must drop both (a stale one from the
        # previously-selected radio must never fire after a switch).
        seen_a, seen_b = [], []
        relay = ComRelay("COM_FAKE", 19200, "127.0.0.1", 0)
        relay.add_cat_listener(seen_a.append)
        relay.add_cat_listener(seen_b.append)

        class _FakeReader:
            def __init__(self, chunks):
                self._chunks = list(chunks)

            async def read(self, n):
                return self._chunks.pop(0) if self._chunks else b""

        relay.serial = mock.Mock()
        task = asyncio.create_task(relay._pump_tcp_to_serial(_FakeReader([b"\xfe\xfe\xe0\x94\x03\xfd"])))
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert seen_a == [b"\xfe\xfe\xe0\x94\x03\xfd"] == seen_b

        # Turning RC-28 off must drop only its own listener — the radio
        # panel's must keep receiving replies undisturbed.
        relay.remove_cat_listener(seen_a.append)
        assert relay.cat_listeners == [seen_b.append]

        relay.clear_cat_listeners()
        assert relay.cat_listeners == []

    asyncio.run(_demo_idle_timeout())
    asyncio.run(_demo_line_state_change())
    asyncio.run(_demo_multiple_cat_listeners())
    print("com_relay.py: ok")
