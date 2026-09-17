"""Drives the built-in radio panel's (client/radio_panel.py) frequency,
mode, S-meter, and the extra rig controls (TUNER/P.AMP/AGC/NB/NR/NOTCH/
COMP/TPF/TONE, AF/RF/MIC/PWR/CW-PITCH dials, split, attenuator, TX/RX
status, TX output power) over the same CAT relay real software uses (see
client/com_relay.py) — no asyncio.Queue or Qt here, same split as
client/rc28.py: this is the testable logic, radio_panel.py is the
Qt shell that reads its plain attributes on a timer tick.

The extra controls beyond frequency/mode/S-meter are per radio model
(see common/civ_models.py) — an unlisted model just means those controls
stay unset/unsupported here, and radio_panel.py leaves the matching
buttons/dials disabled. Nothing guesses a command for a model that
hasn't been looked up in its own manual.

CI-V has no subscribe/push — only request/reply — so staying in sync
with the radio (including changes made on its own front panel, or by
another CAT app sharing the same passthrough) means polling.
"""

import asyncio
import logging

from common.civ import (
    attenuator_command, extract_frames, get_attenuator_command, get_frequency_command,
    get_level_command, get_mode_command, get_single_byte_command, get_smeter_command,
    get_split_command, level_command, parse_attenuator_reply, parse_frequency_reply,
    parse_level_reply, parse_mode_reply, parse_single_byte_reply, parse_smeter_reply,
    parse_split_reply, set_frequency_command, set_mode_command, single_byte_command,
    split_command,
)
from common.civ_models import MODELS

log = logging.getLogger("radio_panel_ctl")

POLL_INTERVAL_S = 0.5  # a human operating a panel, not a spectrum scope

PTT_STATUS_CMD, PTT_STATUS_SUB = 0x1C, 0x00  # same command PTT itself uses — no data byte = a query


class RadioPanelController:
    def __init__(self, send_cat, civ_address: int, model: str | None = None):
        self.send_cat = send_cat  # async callable(bytes) -> None, e.g. ComRelay.send_cat
        self.civ_address = civ_address
        self.commands = MODELS.get(model, {"toggles": {}, "levels": {}})
        self.frequency_hz = None
        self.mode = None        # Icom mode byte — see common.civ.MODE_NAMES
        self.filter_num = None
        self.smeter = None      # raw 0-255 CI-V meter reading
        self.transmitting = None  # None=unknown yet, True=TX, False=RX
        self.split = None       # None=unknown yet, else bool
        self.attenuator_db = None  # None=unknown yet, else the raw CI-V byte (0x00/0x20)
        # name -> current raw value (0-255, or 0/1/2/3 for toggles/enums)
        # or None if never polled yet / model doesn't support it.
        self.toggles = {name: None for name in self.commands["toggles"]}
        self.levels = {name: None for name in self.commands["levels"]}
        self._buf = bytearray()
        self._poll_names = (
            ["frequency", "mode", "smeter", "ptt", "split", "attenuator"]
            + list(self.toggles) + list(self.levels)
        )

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
            return
        ptt = parse_single_byte_reply(frame, PTT_STATUS_CMD, PTT_STATUS_SUB)
        if ptt is not None:
            self.transmitting = ptt == 0x01
            return
        split = parse_split_reply(frame)
        if split is not None:
            self.split = split
            return
        att = parse_attenuator_reply(frame)
        if att is not None:
            self.attenuator_db = att
            return
        for name, (cmd, sub) in self.commands["toggles"].items():
            val = parse_single_byte_reply(frame, cmd, sub)
            if val is not None:
                self.toggles[name] = val
                return
        for name, (cmd, sub) in self.commands["levels"].items():
            val = parse_level_reply(frame, cmd, sub)
            if val is not None:
                self.levels[name] = val
                return

    async def set_frequency(self, freq_hz: int):
        self.frequency_hz = freq_hz  # optimistic; the next poll corrects it if the radio rejected it
        await self.send_cat(set_frequency_command(self.civ_address, freq_hz))

    async def set_mode(self, mode: int, filter_num: int = 1):
        self.mode, self.filter_num = mode, filter_num
        await self.send_cat(set_mode_command(self.civ_address, mode, filter_num))

    async def set_toggle(self, name: str, value: int):
        if name not in self.commands["toggles"]:
            return  # not supported on this model — radio_panel.py keeps the control disabled
        self.toggles[name] = value
        cmd, sub = self.commands["toggles"][name]
        await self.send_cat(single_byte_command(self.civ_address, cmd, sub, value))

    async def set_level(self, name: str, value: int):
        if name not in self.commands["levels"]:
            return
        self.levels[name] = value
        cmd, sub = self.commands["levels"][name]
        await self.send_cat(level_command(self.civ_address, cmd, sub, value))

    async def set_split(self, on: bool):
        self.split = on
        await self.send_cat(split_command(self.civ_address, on))

    async def set_attenuator(self, on: bool):
        self.attenuator_db = 0x20 if on else 0x00
        await self.send_cat(attenuator_command(self.civ_address, self.attenuator_db))

    async def _poll_one(self, name: str):
        if name == "frequency":
            await self.send_cat(get_frequency_command(self.civ_address))
        elif name == "mode":
            await self.send_cat(get_mode_command(self.civ_address))
        elif name == "smeter":
            await self.send_cat(get_smeter_command(self.civ_address))
        elif name == "ptt":
            await self.send_cat(get_single_byte_command(self.civ_address, PTT_STATUS_CMD, PTT_STATUS_SUB))
        elif name == "split":
            await self.send_cat(get_split_command(self.civ_address))
        elif name == "attenuator":
            await self.send_cat(get_attenuator_command(self.civ_address))
        elif name in self.toggles:
            cmd, sub = self.commands["toggles"][name]
            await self.send_cat(get_single_byte_command(self.civ_address, cmd, sub))
        elif name in self.levels:
            cmd, sub = self.commands["levels"][name]
            await self.send_cat(get_level_command(self.civ_address, cmd, sub))

    async def run(self):
        # INFO, not DEBUG: this line alone in client.log proves the polling
        # loop actually started (civ_address configured, relay was up) —
        # the previous silence made "nothing updates" indistinguishable
        # from "never even tried" and "radio never replies".
        log.info(
            "radio panel CAT polling started (CI-V %02X, %d extra controls)",
            self.civ_address, len(self.toggles) + len(self.levels),
        )
        # One full pass over _poll_names always takes POLL_INTERVAL_S total
        # (interval shrinks as more controls are added), so the silent-cycle
        # count below still means the same thing regardless of model.
        interval = POLL_INTERVAL_S / len(self._poll_names)
        silent_cycles = 0
        warned = False
        while True:
            for name in self._poll_names:
                await self._poll_one(name)
                await asyncio.sleep(interval)
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

    ctl = RadioPanelController(fake_send, 0x94, "IC-7300")
    assert ctl.frequency_hz is None and ctl.mode is None and ctl.smeter is None
    assert ctl.toggles == {name: None for name in ("tuner", "preamp", "agc", "noise_blanker",
                                                     "noise_reduction", "auto_notch", "repeater_tone",
                                                     "compressor", "twin_peak_filter")}
    assert ctl.levels == {name: None for name in ("af_gain", "rf_gain", "cw_pitch", "rf_power",
                                                    "mic_gain", "po_meter")}

    # An unlisted model must degrade gracefully — no commands, nothing crashes.
    ctl_unknown = RadioPanelController(fake_send, 0x70, "IC-9700-NOT-YET-ADDED")
    assert ctl_unknown.toggles == {} and ctl_unknown.levels == {}
    asyncio.run(ctl_unknown.set_toggle("tuner", 1))  # must be a silent no-op, not KeyError
    assert sent == []

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

    # TX/RX status (cmd 1C 00) — same shape a PTT set-command has, but
    # this is a reply FROM the radio (from=civ_address), not our own write.
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x1c\x00\x01" + END)
    assert ctl.transmitting is True
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x1c\x00\x00" + END)
    assert ctl.transmitting is False

    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x0f\x01" + END)
    assert ctl.split is True

    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x11\x20" + END)
    assert ctl.attenuator_db == 0x20

    # A model-specific toggle (AGC, 16 12) and level (RF power, 14 0A).
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x16\x12\x02" + END)  # MID
    assert ctl.toggles["agc"] == 2
    ctl.on_cat_reply(PREAMBLE + CONTROLLER_ADDR + b"\x94\x14\x0a\x01\x28" + END)  # 128 raw
    assert ctl.levels["rf_power"] == 128

    ctl.on_cat_reply(b"garbage")  # must not raise, and must not touch existing state
    assert ctl.frequency_hz == 14250000

    # A reply split across several reads — the exact failure mode a slow
    # serial read produces on real hardware — must still be recognized.
    ctl2 = RadioPanelController(fake_send, 0x94, "IC-7300")
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

    asyncio.run(ctl.set_toggle("tuner", 1))
    assert ctl.toggles["tuner"] == 1
    assert sent[-1] == single_byte_command(0x94, 0x1C, 0x01, 1)

    asyncio.run(ctl.set_level("mic_gain", 200))
    assert ctl.levels["mic_gain"] == 200
    assert sent[-1] == level_command(0x94, 0x14, 0x0B, 200)

    asyncio.run(ctl.set_split(True))
    assert ctl.split is True
    assert sent[-1] == split_command(0x94, True)

    asyncio.run(ctl.set_attenuator(True))
    assert ctl.attenuator_db == 0x20
    assert sent[-1] == attenuator_command(0x94, 0x20)
    asyncio.run(ctl.set_attenuator(False))
    assert ctl.attenuator_db == 0x00

    print("radio_panel_ctl.py: ok")
