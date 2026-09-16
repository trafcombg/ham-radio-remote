"""Minimal Icom CI-V command helpers: PTT, operating frequency, mode,
and S-meter — set/get operating frequency was originally for the RC-28
driver (dial deltas); mode and S-meter are for the built-in radio panel
(client/radio_panel.py), an alternative to opening a separate CAT app."""

PREAMBLE = b"\xfe\xfe"
CONTROLLER_ADDR = b"\xe0"
END = b"\xfd"

# Standard Icom mode byte (cmd 0x04/0x06) -> name. Covers the common
# rigs this project targets (IC-7300/IC-746PRO); DV/DD (D-STAR) and
# other data-radio-specific modes are deliberately left out.
MODE_NAMES = {0x00: "LSB", 0x01: "USB", 0x02: "AM", 0x03: "CW", 0x04: "RTTY", 0x05: "FM", 0x07: "CW-R", 0x08: "RTTY-R"}
NAME_TO_MODE = {name: code for code, name in MODE_NAMES.items()}


def ptt_command(civ_address: int, on: bool) -> bytes:
    addr = bytes([civ_address])
    state = b"\x01" if on else b"\x00"
    return PREAMBLE + addr + CONTROLLER_ADDR + b"\x1c\x00" + state + END


def parse_ptt_command(data: bytes, civ_address: int) -> bool | None:
    """Recognizes a CI-V PTT frame addressed to `civ_address` in a raw byte
    chunk (the 'from' controller address is ignored — any CAT app can use
    any address there) — used to detect PTT commands a third-party program
    sends through the CAT passthrough, not just our own. Returns True/False
    for on/off, or None if `data` isn't exactly that frame."""
    if (
        len(data) != 8
        or data[0:2] != PREAMBLE
        or data[2] != civ_address
        or data[4:6] != b"\x1c\x00"
        or data[7:8] != END
    ):
        return None
    return data[6] == 0x01


def get_frequency_command(civ_address: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x03" + END


def set_frequency_command(civ_address: int, freq_hz: int) -> bytes:
    """Standard Icom CI-V 'set operating frequency' (cmd 0x05): the
    frequency as 5 BCD bytes, least-significant byte first, each byte's
    two nibbles a decimal digit pair."""
    digits = f"{freq_hz:010d}"
    bcd = bytes((int(digits[8 - 2 * i]) << 4) | int(digits[9 - 2 * i]) for i in range(5))
    addr = bytes([civ_address])
    return PREAMBLE + addr + CONTROLLER_ADDR + b"\x05" + bcd + END


def parse_frequency_reply(data: bytes) -> int | None:
    """Parses an Icom CI-V operating-frequency reply frame (radio ->
    controller, after cmd 0x03, or unsolicited) back into Hz. Returns
    None if `data` isn't a well-formed frequency reply frame."""
    if len(data) < 11 or data[0:2] != PREAMBLE or data[4] != 0x03 or data[-1:] != END:
        return None
    bcd = data[5:10]
    pairs = [f"{b:02x}" for b in bcd]  # a valid BCD byte's hex digits ARE its decimal digits
    return int("".join(reversed(pairs)))


def get_mode_command(civ_address: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x04" + END


def set_mode_command(civ_address: int, mode: int, filter_num: int = 1) -> bytes:
    addr = bytes([civ_address])
    return PREAMBLE + addr + CONTROLLER_ADDR + b"\x06" + bytes([mode, filter_num]) + END


def parse_mode_reply(data: bytes) -> tuple[int, int | None] | None:
    """(mode, filter) from a cmd-0x04 reply. `filter` is None on rigs
    that only echo the mode byte (the filter byte is optional in the
    Icom CI-V spec, unlike frequency's fixed-width BCD)."""
    if len(data) not in (7, 8) or data[0:2] != PREAMBLE or data[4] != 0x04 or data[-1:] != END:
        return None
    return data[5], (data[6] if len(data) == 8 else None)


def get_smeter_command(civ_address: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x15\x02" + END


def parse_smeter_reply(data: bytes) -> int | None:
    """0-255 raw S-meter reading from a cmd-0x15/0x02 reply — big-endian
    BCD (unlike frequency's little-endian byte order), 0000-0255."""
    if len(data) != 9 or data[0:2] != PREAMBLE or data[4] != 0x15 or data[5] != 0x02 or data[-1:] != END:
        return None
    return int(f"{data[6]:02x}{data[7]:02x}")


if __name__ == "__main__":
    # ponytail-required self-check for non-trivial logic (byte packing).
    assert ptt_command(0x94, True) == b"\xfe\xfe\x94\xe0\x1c\x00\x01\xfd"
    assert ptt_command(0x94, False) == b"\xfe\xfe\x94\xe0\x1c\x00\x00\xfd"

    assert parse_ptt_command(ptt_command(0x94, True), 0x94) is True
    assert parse_ptt_command(ptt_command(0x94, False), 0x94) is False
    assert parse_ptt_command(ptt_command(0x94, True), 0x99) is None  # different radio's address
    # a real app may use a controller address other than E0 — must not matter
    assert parse_ptt_command(b"\xfe\xfe\x94\x00\x1c\x00\x01\xfd", 0x94) is True
    assert parse_ptt_command(b"garbage", 0x94) is None
    assert parse_ptt_command(set_frequency_command(0x94, 7000000), 0x94) is None  # different command, not PTT

    assert set_frequency_command(0x94, 7000000) == b"\xfe\xfe\x94\xe0\x05\x00\x00\x00\x07\x00\xfd"
    assert set_frequency_command(0x94, 14250000) == b"\xfe\xfe\x94\xe0\x05\x00\x00\x25\x14\x00\xfd"

    cmd = set_frequency_command(0x94, 14250000)
    bcd = cmd[5:10]
    reply = b"\xfe\xfe\xe0\x94\x03" + bcd + b"\xfd"
    assert parse_frequency_reply(reply) == 14250000
    assert parse_frequency_reply(b"garbage") is None

    assert get_frequency_command(0x94) == b"\xfe\xfe\x94\xe0\x03\xfd"

    assert MODE_NAMES[0x01] == "USB" and NAME_TO_MODE["USB"] == 0x01
    assert get_mode_command(0x94) == b"\xfe\xfe\x94\xe0\x04\xfd"
    assert set_mode_command(0x94, 0x01, 1) == b"\xfe\xfe\x94\xe0\x06\x01\x01\xfd"
    assert parse_mode_reply(b"\xfe\xfe\xe0\x94\x04\x01\x01\xfd") == (0x01, 1)
    assert parse_mode_reply(b"\xfe\xfe\xe0\x94\x04\x01\xfd") == (0x01, None)  # no filter byte
    assert parse_mode_reply(b"garbage") is None

    assert get_smeter_command(0x94) == b"\xfe\xfe\x94\xe0\x15\x02\xfd"
    assert parse_smeter_reply(b"\xfe\xfe\xe0\x94\x15\x02\x01\x40\xfd") == 140
    assert parse_smeter_reply(b"\xfe\xfe\xe0\x94\x15\x02\x02\x41\xfd") == 241  # S9+60
    assert parse_smeter_reply(b"garbage") is None

    print("civ.py: ok")
