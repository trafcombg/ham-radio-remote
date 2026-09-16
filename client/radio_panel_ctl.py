"""Drives the built-in radio panel's (client/radio_panel.py) frequency,
mode, and S-meter over the same CAT relay real software uses (see
client/com_relay.py) — no asyncio.Queue or Qt here, same split as
client/rc28.py: this is the testable logic, radio_panel.py is the
Qt shell that reads its plain attributes on a timer tick.

CI-V has no subscribe/push — only request/reply — so staying in sync
with the radio (including changes made on its own front panel, or by
another CAT app sharing the same passthrough) means polling.
"""

import asyncio
import logging

from common.civ import (
    extract_frames, get_frequency_command, get_mode_command, get_smeter_command,
    parse_frequency_reply, parse_mode_reply, parse_smeter_reply,
    set_frequency_command, set_mode_command,
)

log = logging.getLogger("radio_panel_ctl")

POLL_INTERVAL_S = 0.5  # a human operating a panel, not a spectrum scope


class RadioPanelController:
    def __init__(self, send_cat, civ_address: int):
        self.send_cat = send_cat  # async callable(bytes) -> None, e.g. ComRelay.send_cat
        self.civ_address = civ_address
        self.frequency_hz = None
        self.mode = None        # Icom mode byte — see common.civ.MODE_NAMES
        self.filter_num = None
        self.smeter = None      # raw 0-255 CI-V meter reading
        self._buf = bytearray()

    def on_cat_reply(self, data: bytes):
        """Feed this from ComRelay.add_cat_listener to keep our state in
        sync with whatever the radio last reported — including replies
        to our own polls below, and anything a third-party CAT app
        sharing the same passthrough triggers. A single reply can arrive
        split across multiple calls (slow serial reads don't land on
        frame boundaries) — extract_frames() reassembles it."""
        for frame in extract_frames(self._buf, data):
            self._handle_frame(frame)

    def _handle_frame(self, frame: bytes):
        freq = parse_frequency_reply(frame)
        if freq is not None:
            self.frequency_hz = freq
            return
        mode = parse_mode_reply(frame)
        if mode is not None:
            self.mode, self.filter_num = mode
            return
        meter = parse_smeter_reply(frame)
        if meter is not None:
            self.smeter = meter

    async def set_frequency(self, freq_hz: int):
        self.frequency_hz = freq_hz  # optimistic; the next poll corrects it if the radio rejected it
        await self.send_cat(set_frequency_command(self.civ_address, freq_hz))

    async def set_mode(self, mode: int, filter_num: int = 1):
        self.mode, self.filter_num = mode, filter_num
        await self.send_cat(set_mode_command(self.civ_address, mode, filter_num))

    async def run(self):
        # INFO, not DEBUG: this line alone in client.log proves the polling
        # loop actually started (civ_address configured, relay was up) —
        # the previous silence made "nothing updates" indistinguishable
        # from "never even tried" and "radio never replies".
        log.info("radio panel CAT polling started (CI-V %02X)", self.civ_address)
        silent_cycles = 0
        warned = False
        while True:
            await self.send_cat(get_frequency_command(self.civ_address))
            await asyncio.sleep(POLL_INTERVAL_S / 3)
            await self.send_cat(get_mode_command(self.civ_address))
            await asyncio.sleep(POLL_INTERVAL_S / 3)
            await self.send_cat(get_smeter_command(self.civ_address))
            await asyncio.sleep(POLL_INTERVAL_S / 3)
            if self.frequency_hz is None:
                silent_cycles += 1
                if silent_cycles == 10 and not warned:
                    warned = True
                    log.warning(
                        "no CI-V reply from the radio after ~%ds (CI-V %02X) — "
                        "check the radio's own CI-V address/baud and that CI-V "
                        "transceive/remote is enabled on the radio itself",
                        round(10 * POLL_INTERVAL_S), self.civ_address,
                    )
            else:
                silent_cycles = 0
                warned = False


if __name__ == "__main__":
    from common.civ import CONTROLLER_ADDR, END, PREAMBLE

    sent = []

    async def fake_send(data):
        sent.append(data)

    ctl = RadioPanelController(fake_send, 0x94)
    assert ctl.frequency_hz is None and ctl.mode is None and ctl.smeter is None

    # Frames as the radio would send them: PREAMBLE, to=E0, from=<its
    # own civ_address>, then the same payload our own get_* commands ask for.
    freq_reply = PREAMBLE + CONTROLLER_ADDR + b"\x94\x03\x00\x00\x25\x14\x00" + END
    ctl.on_cat_reply(freq_reply)
    assert ctl.frequency_hz == 14250000

    mode_reply = PREAMBLE + CONTROLLER_ADDR + b"\x94\x04\x01\x01" + END  # USB, filter 1
    ctl.on_cat_reply(mode_reply)
    assert ctl.mode == 0x01 and ctl.filter_num == 1

    meter_reply = PREAMBLE + CONTROLLER_ADDR + b"\x94\x15\x02\x01\x40" + END  # 0140 raw
    ctl.on_cat_reply(meter_reply)
    assert ctl.smeter == 140

    ctl.on_cat_reply(b"garbage")  # must not raise, and must not touch existing state
    assert ctl.frequency_hz == 14250000

    # A reply split across several reads — the exact failure mode a slow
    # serial read produces on real hardware — must still be recognized.
    ctl2 = RadioPanelController(fake_send, 0x94)
    ctl2.on_cat_reply(mode_reply[:3])
    ctl2.on_cat_reply(mode_reply[3:6])
    ctl2.on_cat_reply(mode_reply[6:])
    assert ctl2.mode == 0x01 and ctl2.filter_num == 1, "fragmented reply was never reassembled"

    asyncio.run(ctl.set_frequency(7000000))
    assert ctl.frequency_hz == 7000000
    assert sent[-1] == set_frequency_command(0x94, 7000000)

    asyncio.run(ctl.set_mode(0x03, 1))  # CW
    assert ctl.mode == 0x03 and ctl.filter_num == 1
    assert sent[-1] == set_mode_command(0x94, 0x03, 1)

    print("radio_panel_ctl.py: ok")
