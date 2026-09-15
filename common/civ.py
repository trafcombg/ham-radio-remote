"""Minimal Icom CI-V command helpers (PTT only — Phase 1 scope)."""

PREAMBLE = b"\xfe\xfe"
CONTROLLER_ADDR = b"\xe0"
END = b"\xfd"


def ptt_command(civ_address: int, on: bool) -> bytes:
    addr = bytes([civ_address])
    state = b"\x01" if on else b"\x00"
    return PREAMBLE + addr + CONTROLLER_ADDR + b"\x1c\x00" + state + END


if __name__ == "__main__":
    # ponytail-required self-check for non-trivial logic (byte packing).
    assert ptt_command(0x94, True) == b"\xfe\xfe\x94\xe0\x1c\x00\x01\xfd"
    assert ptt_command(0x94, False) == b"\xfe\xfe\x94\xe0\x1c\x00\x00\xfd"
    print("civ.py: ok")
