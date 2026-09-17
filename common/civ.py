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


def extract_frames(buffer: bytearray, data: bytes) -> list[bytes]:
    """Feeds raw bytes from a CAT stream into `buffer` and pulls out zero
    or more complete FE FE ... FD frames. A serial/TCP read is NOT
    guaranteed to land on frame boundaries — a single CI-V reply can
    arrive split across several reads (slow baud rate, USB-serial chip
    buffering), or several replies can arrive coalesced in one read — so
    callers that parse a raw chunk directly (exact-length checks like
    parse_frequency_reply) silently miss any reply that isn't lucky
    enough to arrive as one whole chunk. `buffer` is mutated/trimmed in
    place so the next call picks up exactly where this one left off;
    leading bytes before the first FE FE (noise, or a frame this radio's
    address doesn't own) are dropped, not queued forever."""
    buffer.extend(data)
    frames = []
    while True:
        start = buffer.find(PREAMBLE)
        if start == -1:
            buffer.clear()
            break
        end = buffer.find(END, start)
        if end == -1:
            del buffer[:start]  # keep the partial frame, wait for more
            break
        frames.append(bytes(buffer[start:end + 1]))
        del buffer[:end + 1]
    return frames


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


def _bcd2(value: int) -> bytes:
    """2-byte BCD encoding of a 0-255 value, e.g. 255 -> b'\\x02\\x55' —
    the same format the radio uses for S-meter and the 'levels' below
    (AF/RF/mic gain, CW pitch, PO/SWR/ALC meters...), per the IC-7300
    Full Manual's CI-V command table (section 19)."""
    s = f"{value:04d}"
    return bytes([(int(s[0]) << 4) | int(s[1]), (int(s[2]) << 4) | int(s[3])])


def _parse_bcd2(hi: int, lo: int) -> int:
    return int(f"{hi:02x}{lo:02x}")


def single_byte_command(civ_address: int, cmd: int, sub: int, value: int) -> bytes:
    """Generic 'send a one-byte setting' CI-V frame — covers both plain
    on/off toggles (value 0/1) and small enums like AGC's FAST/MID/SLOW
    (value 1/2/3), which the radio encodes identically: cmd, sub-cmd,
    one raw data byte. See common/civ_models.py for which (cmd, sub)
    pairs a given radio model actually supports."""
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + bytes([cmd, sub, value]) + END


def get_single_byte_command(civ_address: int, cmd: int, sub: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + bytes([cmd, sub]) + END


def parse_single_byte_reply(data: bytes, cmd: int, sub: int) -> int | None:
    if len(data) != 8 or data[0:2] != PREAMBLE or data[4] != cmd or data[5] != sub or data[-1:] != END:
        return None
    return data[6]


def level_command(civ_address: int, cmd: int, sub: int, value: int) -> bytes:
    """Generic 'send a 0-255 level' CI-V frame (AF/RF/mic gain, CW pitch,
    RF power...) — same cmd+sub addressing as single_byte_command, but a
    2-byte BCD value instead of one raw byte (see _bcd2)."""
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + bytes([cmd, sub]) + _bcd2(value) + END


def get_level_command(civ_address: int, cmd: int, sub: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + bytes([cmd, sub]) + END


def parse_level_reply(data: bytes, cmd: int, sub: int) -> int | None:
    if len(data) != 9 or data[0:2] != PREAMBLE or data[4] != cmd or data[5] != sub or data[-1:] != END:
        return None
    return _parse_bcd2(data[6], data[7])


def split_command(civ_address: int, on: bool) -> bytes:
    """Split (cmd 0x0F) has no sub-command byte, unlike the 0x14/0x15/0x16
    families above — same shape on every CI-V radio, not model-specific."""
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x0f" + bytes([1 if on else 0]) + END


def get_split_command(civ_address: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x0f" + END


def parse_split_reply(data: bytes) -> bool | None:
    if len(data) != 7 or data[0:2] != PREAMBLE or data[4] != 0x0f or data[-1:] != END:
        return None
    return data[5] == 0x01


def attenuator_command(civ_address: int, raw_byte: int) -> bytes:
    """Attenuator (cmd 0x11) also has no sub-command byte, and its data
    byte is a raw hex value (0x00=OFF, 0x20=20dB ON on the IC-7300) —
    not a 0/1 flag, and not decimal dB — universal across CI-V radios,
    not model-specific."""
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x11" + bytes([raw_byte]) + END


def get_attenuator_command(civ_address: int) -> bytes:
    return PREAMBLE + bytes([civ_address]) + CONTROLLER_ADDR + b"\x11" + END


def parse_attenuator_reply(data: bytes) -> int | None:
    if len(data) != 7 or data[0:2] != PREAMBLE or data[4] != 0x11 or data[-1:] != END:
        return None
    return data[5]


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

    # single_byte_command family — toggles (AGC 16 12, etc.), verified
    # byte-for-byte against the IC-7300 Full Manual's CI-V command table.
    assert single_byte_command(0x94, 0x16, 0x02, 1) == b"\xfe\xfe\x94\xe0\x16\x02\x01\xfd"
    assert get_single_byte_command(0x94, 0x16, 0x02) == b"\xfe\xfe\x94\xe0\x16\x02\xfd"
    assert parse_single_byte_reply(b"\xfe\xfe\xe0\x94\x16\x02\x01\xfd", 0x16, 0x02) == 1
    assert parse_single_byte_reply(b"\xfe\xfe\xe0\x94\x16\x03\x01\xfd", 0x16, 0x02) is None  # wrong sub-cmd
    assert parse_single_byte_reply(b"garbage", 0x16, 0x02) is None

    # level_command family (0-255, 2-byte BCD) — e.g. RF power 14 0A.
    assert level_command(0x94, 0x14, 0x0A, 255) == b"\xfe\xfe\x94\xe0\x14\x0a\x02\x55\xfd"
    assert get_level_command(0x94, 0x14, 0x0A) == b"\xfe\xfe\x94\xe0\x14\x0a\xfd"
    assert parse_level_reply(b"\xfe\xfe\xe0\x94\x14\x0a\x02\x55\xfd", 0x14, 0x0A) == 255
    assert parse_level_reply(b"\xfe\xfe\xe0\x94\x14\x0b\x02\x55\xfd", 0x14, 0x0A) is None  # wrong sub-cmd

    assert split_command(0x94, True) == b"\xfe\xfe\x94\xe0\x0f\x01\xfd"
    assert get_split_command(0x94) == b"\xfe\xfe\x94\xe0\x0f\xfd"
    assert parse_split_reply(b"\xfe\xfe\xe0\x94\x0f\x01\xfd") is True
    assert parse_split_reply(b"\xfe\xfe\xe0\x94\x0f\x00\xfd") is False
    assert parse_split_reply(b"garbage") is None

    # Attenuator's data byte is a raw hex value (0x20), not decimal 20 —
    # same hex-literal convention as every cmd/sub-cmd byte in the manual
    # (it just happens to read as "20" either way).
    assert attenuator_command(0x94, 0x20) == b"\xfe\xfe\x94\xe0\x11\x20\xfd"
    assert get_attenuator_command(0x94) == b"\xfe\xfe\x94\xe0\x11\xfd"
    assert parse_attenuator_reply(b"\xfe\xfe\xe0\x94\x11\x20\xfd") == 0x20
    assert parse_attenuator_reply(b"\xfe\xfe\xe0\x94\x11\x00\xfd") == 0
    assert parse_attenuator_reply(b"garbage") is None

    freq_frame = b"\xfe\xfe\xe0\x94\x03\x00\x00\x25\x14\x00\xfd"
    buf = bytearray()
    assert extract_frames(buf, freq_frame) == [freq_frame]
    assert buf == bytearray()  # fully consumed, nothing left dangling

    # Split across three reads, at arbitrary byte boundaries — the exact
    # failure mode a slow/chunked serial read produces on real hardware.
    buf = bytearray()
    assert extract_frames(buf, freq_frame[:3]) == []
    assert extract_frames(buf, freq_frame[3:7]) == []
    assert extract_frames(buf, freq_frame[7:]) == [freq_frame]

    # Two replies coalesced into a single read.
    buf = bytearray()
    mode_frame = b"\xfe\xfe\xe0\x94\x04\x01\x01\xfd"
    assert extract_frames(buf, freq_frame + mode_frame) == [freq_frame, mode_frame]

    # Noise before a real frame must be dropped, not queued forever.
    buf = bytearray()
    assert extract_frames(buf, b"\x00\x01" + freq_frame) == [freq_frame]

    print("civ.py: ok")
