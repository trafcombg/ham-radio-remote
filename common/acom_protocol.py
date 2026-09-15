"""ACOM 500S/600S/700S/1200S/2020S RS-232 remote-control protocol.

CONFIRMED, not guessed: command bytes and telemetry frame layout are
taken directly from bjornekelund/ACOM-Controller (github.com/bjornekelund/
ACOM-Controller), an actively used open-source Windows app for these
amplifiers — exactly the reference the project plan points at. The SWR
scaling (raw/100) is independently cross-confirmed by a second project,
pingpongshow/AcomControl (ESP32 replacement for eBox).

9600 baud, 8N1, no handshake, DTR and RTS held low (the amp uses those
lines for its own front-panel power button, per the source project).

Every command below and the telemetry frame both use the same checksum
convention: the bytes sum to 0 mod 256. That's asserted in the self-test.

What this module does NOT cover: how ACOM's eBox (the Ethernet bridge)
exposes this over HTTP — that protocol isn't publicly documented, and
confirming it needs a live capture (Chrome DevTools -> Network tab) of a
real eBox, which this environment doesn't have. See server/ebox_transport.py.
"""

BAUD = 9600

CMD_ENABLE_TELEMETRY = bytes([0x55, 0x92, 0x04, 0x15])
CMD_DISABLE_TELEMETRY = bytes([0x55, 0x91, 0x04, 0x16])
CMD_OPERATE = bytes([0x55, 0x81, 0x08, 0x02, 0x00, 0x06, 0x00, 0x1A])
CMD_STANDBY = bytes([0x55, 0x81, 0x08, 0x02, 0x00, 0x05, 0x00, 0x1B])
CMD_OFF = bytes([0x55, 0x81, 0x08, 0x02, 0x00, 0x0A, 0x00, 0x16])

MSG_LEN = 72

STATUS_NAMES = {
    1: "reset", 2: "init", 3: "debug", 4: "service", 5: "standby",
    6: "receive", 7: "transmit", 9: "system", 10: "off",
}

BAND_NAMES = [
    "?m", "160m", "80m", "40/60m", "30m", "20m",
    "17m", "15m", "12m", "10m", "6m", "4m", "?m", "?m", "?m", "?m",
]

# Only the temperature offset is actually used (decode_telemetry below);
# add nominal/max power fields back here if a UI ever needs them for scaling.
MODEL_TEMP_OFFSET = {"500S": 282, "600S": 273, "700S": 282, "1200S": 281, "2020S": 282}


def decode_telemetry(frame: bytes, model: str = "1200S") -> dict:
    """frame must be exactly MSG_LEN bytes with a valid checksum (see
    TelemetryParser, which only ever calls this on validated frames)."""
    temp_offset = MODEL_TEMP_OFFSET[model]
    status_code = (frame[3] & 0xF0) >> 4
    return {
        "status": STATUS_NAMES.get(status_code, "unknown"),
        "temp_c": frame[16] + frame[17] * 256 - temp_offset,
        "fan": (frame[69] & 0xF0) >> 4,
        "band": BAND_NAMES[frame[69] & 0x0F],
        "drive_power_w": (frame[20] + frame[21] * 256) / 10.0,
        "output_power_w": frame[22] + frame[23] * 256,
        "reflected_power_w": frame[24] + frame[25] * 256,
        "swr": (frame[26] + frame[27] * 256) / 100.0,
        "dc_power_w": frame[8] * 0.1 + frame[9] * 25.6,
        "error_code": frame[66],
        "fault": frame[66] != 0xFF,
    }


class TelemetryParser:
    """Stateful byte-stream parser — feed it raw bytes as they arrive
    (from a serial port or a socket, doesn't matter which). Mirrors the
    reference app's own parser: look for 0x55 0x2f, accumulate MSG_LEN
    bytes, keep only frames whose checksum is valid."""

    def __init__(self, model: str = "1200S"):
        self.model = model
        self._buf = bytearray()
        self._parsing = False

    def feed(self, data: bytes) -> list[dict]:
        frames = []
        for b in data:
            if not self._parsing and b == 0x55:
                self._buf = bytearray([b])
                self._parsing = True
            elif self._parsing:
                self._buf.append(b)
                if len(self._buf) == 2 and self._buf[1] != 0x2F:
                    self._parsing = False
                    self._buf = bytearray()
                elif len(self._buf) == MSG_LEN:
                    if sum(self._buf) % 256 == 0:
                        frames.append(decode_telemetry(bytes(self._buf), self.model))
                    self._parsing = False
                    self._buf = bytearray()
        return frames


def _checksum_ok(cmd: bytes) -> bool:
    return sum(cmd) % 256 == 0


def _build_test_frame() -> bytes:
    frame = bytearray(MSG_LEN)
    frame[0], frame[1] = 0x55, 0x2F
    frame[3] = 0x70  # status nibble = 7 -> transmit
    temp_raw = 311  # -> 311 - 281 = 30C for 1200S
    frame[16], frame[17] = temp_raw & 0xFF, temp_raw >> 8
    drive_raw = 500  # -> 50.0W
    frame[20], frame[21] = drive_raw & 0xFF, drive_raw >> 8
    frame[22], frame[23] = 800 & 0xFF, 800 >> 8  # 800W output
    frame[24], frame[25] = 20 & 0xFF, 20 >> 8  # 20W reflected
    swr_raw = 150  # -> 1.50
    frame[26], frame[27] = swr_raw & 0xFF, swr_raw >> 8
    frame[9] = 39  # dc_power_w = 0*0.1 + 39*25.6 = 998.4W
    frame[66] = 0xFF  # no error
    frame[69] = 0x15  # fan=1, band index 5 -> "20m"
    frame[71] = (256 - sum(frame) % 256) % 256  # zero out the checksum
    return bytes(frame)


if __name__ == "__main__":
    for cmd in (CMD_ENABLE_TELEMETRY, CMD_DISABLE_TELEMETRY, CMD_OPERATE, CMD_STANDBY, CMD_OFF):
        assert _checksum_ok(cmd), cmd.hex()

    frame = _build_test_frame()
    assert sum(frame) % 256 == 0

    parser = TelemetryParser(model="1200S")
    results = parser.feed(frame)
    assert len(results) == 1
    d = results[0]
    assert d["status"] == "transmit"
    assert d["temp_c"] == 30
    assert d["drive_power_w"] == 50.0
    assert d["output_power_w"] == 800
    assert d["reflected_power_w"] == 20
    assert d["swr"] == 1.5
    assert abs(d["dc_power_w"] - 998.4) < 1e-9
    assert d["band"] == "20m"
    assert d["fan"] == 1
    assert d["fault"] is False

    # feeding it split across two calls (as a real socket/serial read would) still works
    parser2 = TelemetryParser()
    assert parser2.feed(frame[:30]) == []
    assert len(parser2.feed(frame[30:])) == 1

    # garbage before a real frame is discarded, not fatal
    parser3 = TelemetryParser()
    assert len(parser3.feed(b"\x01\x02\x03" + frame)) == 1

    # a frame with a corrupted checksum is silently dropped, not raised
    bad = bytearray(frame)
    bad[71] ^= 0xFF
    assert TelemetryParser().feed(bytes(bad)) == []

    print("acom_protocol.py: ok")
