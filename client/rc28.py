"""Icom RC-28 driver — USB VID:PID 0c26:001e, interrupt transfers, NOT a
standard HID keyboard/mouse (confirmed by the project plan).

STATUS: parse_report() is an UNVERIFIED PLACEHOLDER. The RC-28 protocol
isn't officially documented, and this environment has neither the
physical device nor a USB capture to confirm the report byte layout —
per the project plan, that needs Wireshark + USBPcap on real
RC-28<->RS-BA1 traffic (see gi1mic/rc28_emulator on GitHub as a reference
starting point). Everything downstream of parse_report() — dial delta ->
new frequency -> CI-V set-frequency command — IS verified (see the
self-test and common/civ.py's own self-test), so correcting
parse_report() against a real capture is the only thing standing between
this and a working driver.
"""

import asyncio
import logging

from common.civ import extract_frames, parse_frequency_reply, set_frequency_command

log = logging.getLogger("rc28")

VID, PID = 0x0C26, 0x001E
POLL_INTERVAL_S = 0.005  # nonblocking-read poll rate — fast enough a dial click never feels laggy


def parse_report(report: bytes) -> dict:
    """UNVERIFIED PLACEHOLDER. Assumed (unconfirmed) shape: byte[1] =
    signed dial delta since the last report, byte[2] = button bitmask.
    Replace against a real USB capture before trusting this."""
    if len(report) < 3:
        return {"dial_delta": 0, "buttons": 0}
    delta = report[1]
    if delta >= 128:
        delta -= 256
    return {"dial_delta": delta, "buttons": report[2]}


def apply_dial_delta(current_freq_hz: int, delta: int, step_hz: int) -> int:
    return current_freq_hz + delta * step_hz


class Rc28Driver:
    """Wires a physical RC-28 to CAT frequency changes on the currently
    selected radio, through the client's existing CAT relay connection —
    the same tunnel real CAT software uses (see client/com_relay.py)."""

    def __init__(self, send_cat, civ_address: int, step_hz: int = 10):
        self.send_cat = send_cat  # async callable(bytes) -> None, e.g. ComRelay.send_cat
        self.civ_address = civ_address
        self.step_hz = step_hz
        self.current_freq_hz = None
        self._stopped = False
        self._buf = bytearray()

    def on_cat_reply(self, data: bytes):
        """Feed this from ComRelay.add_cat_listener to keep our frequency
        baseline in sync with whatever the radio last reported. A single
        reply can arrive split across multiple calls (slow serial reads
        don't land on frame boundaries) — extract_frames() reassembles it."""
        for frame in extract_frames(self._buf, data):
            freq = parse_frequency_reply(frame)
            if freq is not None:
                self.current_freq_hz = freq

    async def handle_report(self, report: bytes):
        parsed = parse_report(report)
        delta = parsed["dial_delta"]
        if delta == 0 or self.current_freq_hz is None:
            return
        self.current_freq_hz = apply_dial_delta(self.current_freq_hz, delta, self.step_hz)
        await self.send_cat(set_frequency_command(self.civ_address, self.current_freq_hz))

    def stop(self):
        """Signals the poll loop to exit and release the USB device on
        its next iteration (within POLL_INTERVAL_S). The client's RC-28
        on/off toggle (client/session.py's set_rc28_enabled) uses this
        instead of relying only on task cancellation, so turning it off
        doesn't depend on the coroutine sitting at exactly the right
        await point."""
        self._stopped = True

    async def _poll_loop(self, dev):
        try:
            while not self._stopped:
                report = dev.read(64)
                if report:
                    await self.handle_report(bytes(report))
                else:
                    await asyncio.sleep(POLL_INTERVAL_S)
        finally:
            dev.close()

    async def run(self):
        """Reads real RC-28 USB interrupt reports via hidapi, nonblocking
        so this coroutine stays cooperative — a blocking hidapi read here
        would freeze the client's entire asyncio loop (audio, PTT, CAT),
        not just RC-28, until the next dial click. Needs the actual
        device and a corrected parse_report() to do anything useful —
        see the module docstring."""
        import hid

        dev = hid.device()
        dev.open(VID, PID)
        dev.set_nonblocking(True)
        log.info("RC-28 opened (VID:PID %04x:%04x)", VID, PID)
        await self._poll_loop(dev)


if __name__ == "__main__":
    assert apply_dial_delta(14250000, 5, 10) == 14250050
    assert apply_dial_delta(14250000, -3, 10) == 14249970
    assert parse_report(b"\x01\x05\x00") == {"dial_delta": 5, "buttons": 0}
    assert parse_report(b"\x01\xfb\x00") == {"dial_delta": -5, "buttons": 0}  # 0xfb = -5 signed

    driver = Rc28Driver(None, 0x94)
    freq_reply = b"\xfe\xfe\xe0\x94\x03\x00\x00\x25\x14\x00\xfd"
    driver.on_cat_reply(freq_reply[:4])
    driver.on_cat_reply(freq_reply[4:])
    assert driver.current_freq_hz == 14250000, "fragmented reply was never reassembled"

    class _FakeHidDevice:
        """Enough of hidapi's nonblocking device surface for _poll_loop —
        read() returns [] when nothing's pending, exactly like the real
        one in nonblocking mode."""

        def __init__(self, reports):
            self._reports = list(reports)
            self.closed = False

        def read(self, n):
            return self._reports.pop(0) if self._reports else []

        def close(self):
            self.closed = True

    async def _demo_poll_loop_processes_and_stops_cleanly():
        # Reproduces the fix: the old run() called hidapi's BLOCKING
        # read() directly inside the coroutine with no await in the idle
        # path — that froze the whole client's asyncio loop (audio, PTT,
        # CAT), not just RC-28, until the next dial click. _poll_loop
        # must stay cooperative (an idle iteration awaits) and must exit
        # on its own once stop() is called, without needing task.cancel().
        sent = []

        async def fake_send(data):
            sent.append(data)

        driver = Rc28Driver(fake_send, 0x94, step_hz=10)
        driver.current_freq_hz = 14250000
        dev = _FakeHidDevice([bytes([0x01, 0x05, 0x00])])  # one +5 dial click
        task = asyncio.create_task(driver._poll_loop(dev))
        await asyncio.sleep(POLL_INTERVAL_S * 6)
        assert driver.current_freq_hz == 14250050, "dial report was never applied"
        assert sent[-1] == set_frequency_command(0x94, 14250050)

        driver.stop()
        await asyncio.wait_for(task, timeout=1.0)  # must exit on its own
        assert dev.closed, "device must be released once the poll loop exits"

    asyncio.run(_demo_poll_loop_processes_and_stops_cleanly())
    print("rc28.py: ok (parse_report() is still an unverified placeholder — see module docstring)")
