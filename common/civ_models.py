"""Per-model CI-V (cmd, sub-cmd) tables for the radio panel's extra rig
controls (client/radio_panel.py) — TUNER/P.AMP/AGC/NB/NR/NOTCH/COMP/TPF/
TONE and the AF/RF/MIC/PWR/CW-PITCH dials. The base commands (frequency,
mode, S-meter, PTT, split, attenuator) are in common/civ.py and are the
same across every Icom CI-V radio, so they don't need a per-model entry
here — only the "16 xx" (toggle) and "14 xx" (level) families vary by
model.

Every (cmd, sub) pair below is copied byte-for-byte from that radio's own
official Full Manual, section 19 "CONTROL COMMAND" printed command
table — not guessed (guessing a write command for a live transmitter is
how RS-BA1's own .ini files ended up useless for this: they only carry
per-model variations, not the fixed opcodes, which turned out to be
genuinely necessary to look up rather than infer).

A model with no entry here just means its extra controls stay disabled
in the radio panel (RadioPanelController falls back to an empty command
set) — nothing breaks, it's the same as before this file existed. Add a
new model the same way once its manual's command table is available.

Two shapes, matching common/civ.py's two generic command families:
  "toggles" — one raw data byte (civ.single_byte_command/parse_single_byte_reply).
              Covers plain on/off switches AND small enums like AGC.
  "levels"  — 2-byte BCD 0-255 (civ.level_command/parse_level_reply).
              Continuous dials and meters.
"""

IC_7300 = {
    "toggles": {
        "tuner": (0x1C, 0x01),           # 00=OFF, 01=ON (02=start tuning, not exposed here)
        "preamp": (0x16, 0x02),          # 00=OFF, 01=Preamp1 (02=Preamp2, not exposed here)
        "agc": (0x16, 0x12),             # 01=FAST, 02=MID, 03=SLOW — enum, not a 0/1 toggle
        "noise_blanker": (0x16, 0x22),
        "noise_reduction": (0x16, 0x40),
        "auto_notch": (0x16, 0x41),
        "repeater_tone": (0x16, 0x42),
        "compressor": (0x16, 0x44),
        "twin_peak_filter": (0x16, 0x4F),
    },
    "levels": {
        "af_gain": (0x14, 0x01),
        "rf_gain": (0x14, 0x02),
        "cw_pitch": (0x14, 0x09),
        "rf_power": (0x14, 0x0A),
        "mic_gain": (0x14, 0x0B),
        "po_meter": (0x15, 0x11),        # read-only: TX output power, 0-255 = 0-100%
    },
}

MODELS = {"IC-7300": IC_7300}


if __name__ == "__main__":
    # ponytail-required self-check: every listed (cmd, sub) must be a
    # plain 2-tuple of ints in 0-255 — catches a copy-paste typo (wrong
    # tuple shape, a stray hex string) before it reaches real hardware.
    for model, commands in MODELS.items():
        assert set(commands) == {"toggles", "levels"}, model
        for group in commands.values():
            for name, pair in group.items():
                assert isinstance(pair, tuple) and len(pair) == 2, f"{model}.{name}"
                cmd, sub = pair
                assert 0 <= cmd <= 0xFF and 0 <= sub <= 0xFF, f"{model}.{name}"

    # No two controls in the same model should share a (cmd, sub) pair —
    # that would make replies ambiguous (RadioPanelController._handle_frame
    # matches by (cmd, sub) alone).
    for model, commands in MODELS.items():
        all_pairs = list(commands["toggles"].values()) + list(commands["levels"].values())
        assert len(all_pairs) == len(set(all_pairs)), f"{model}: duplicate (cmd, sub)"

    print("civ_models.py: ok")
