"""Minimal Icom CI-V command helpers: PTT, and set/get operating
frequency (needed by the RC-28 driver to translate dial deltas)."""

PREAMBLE = b"\xfe\xfe"
CONTROLLER_ADDR = b"\xe0"
END = b"\xfd"


def ptt_command(civ_address: int, on: bool) -> bytes:
    addr = bytes([civ_address])
    state = b"\x01" if on else b"\x00"
    return PREAMBLE + addr + CONTROLLER_ADDR + b"\x1c\x00" + state + END


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


if __name__ == "__main__":
    # ponytail-required self-check for non-trivial logic (byte packing).
    assert ptt_command(0x94, True) == b"\xfe\xfe\x94\xe0\x1c\x00\x01\xfd"
    assert ptt_command(0x94, False) == b"\xfe\xfe\x94\xe0\x1c\x00\x00\xfd"

    assert set_frequency_command(0x94, 7000000) == b"\xfe\xfe\x94\xe0\x05\x00\x00\x00\x07\x00\xfd"
    assert set_frequency_command(0x94, 14250000) == b"\xfe\xfe\x94\xe0\x05\x00\x00\x25\x14\x00\xfd"

    cmd = set_frequency_command(0x94, 14250000)
    bcd = cmd[5:10]
    reply = b"\xfe\xfe\xe0\x94\x03" + bcd + b"\xfd"
    assert parse_frequency_reply(reply) == 14250000
    assert parse_frequency_reply(b"garbage") is None

    print("civ.py: ok")
