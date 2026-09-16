"""RSW8A1ER 8-port antenna switch — RS-232 command protocol.

CONFIRMED, not guessed: reverse-engineered from a real capture of the
switch's own control software driving a real unit (server/serial_sniffer.py
MITM mode), all 8 ports exercised — see the self-test for the exact
frames. Every command below is byte-for-byte what the vendor software
actually sent/received.

Framing (host -> switch): 0x01, then a length byte counting the ASCII
command that follows, then the command itself, terminated by CR (no LF).
The length byte matches the command's own length in every captured
sample (1 for "D"/"R", 2 for "W1".."W8") — this reads as a plain
SOH+LEN+payload framing, not a "query vs action" opcode group, since
that's the only interpretation consistent with commands of different
lengths.

Commands:
  D       -- query device identification. Reply: "D=8A1R"
  R       -- query the currently selected port. Reply: "R=<state>"
  W1..W8  -- select antenna port 1-8. Reply: "R=<state>" (same shape as
             a plain R query — the switch confirms by echoing its new
             state, not a separate ack)

Replies (switch -> host): "<FIELD>=<value>\\r\\n>" — human-readable
ASCII, CRLF-terminated, followed by a '>' prompt char (no line ending
after it in the capture).

The R/W reply's value was, across all 8 ports captured: the port number
repeated twice, followed by a constant "10" suffix (e.g. port 3 ->
"3310"). The port digit is 100% reliable across every sample; the
trailing "10" never varied in this capture (transmit was never keyed
during capture, so a fault/error state was never observed) — treat
anything beyond the leading port digit as opaque/unconfirmed.
"""

PREFIX = b"\x01"
TERMINATOR = b"\r"

PORT_COUNT = 8


def build_command(cmd: str) -> bytes:
    payload = cmd.encode("ascii")
    return PREFIX + bytes([len(payload)]) + payload + TERMINATOR


CMD_QUERY_ID = build_command("D")
CMD_QUERY_STATE = build_command("R")


def select_port_command(port: int) -> bytes:
    if not 1 <= port <= PORT_COUNT:
        raise ValueError(f"port трябва да е 1-{PORT_COUNT}, получено {port}")
    return build_command(f"W{port}")


def normalize_port_labels(labels: list | None) -> list[str]:
    """Pads/truncates to exactly PORT_COUNT strings, so index i is always
    port i+1's label regardless of what the admin form or an older/newer
    config actually sent — callers (DB storage, the client API) can index
    straight in without a bounds check."""
    labels = [str(x) for x in (labels or [])][:PORT_COUNT]
    return labels + [""] * (PORT_COUNT - len(labels))


class ReplyParser:
    """Stateful byte-stream parser for the switch's replies — feed it
    raw bytes as they arrive (serial reads rarely land on a clean
    message boundary). Looks for a complete "<FIELD>=<value>\\r\\n"
    line; the trailing '>' prompt is consumed but carries no data of
    its own, so it's dropped rather than kept in the buffer."""

    def __init__(self):
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[dict]:
        self._buf += data
        replies = []
        while b"\r\n" in self._buf:
            line, _, rest = self._buf.partition(b"\r\n")
            self._buf = bytearray(rest)
            if self._buf[:1] == b">":
                del self._buf[0:1]  # drop the prompt char, not part of the next line
            parsed = _parse_line(bytes(line))
            if parsed:
                replies.append(parsed)
        return replies


def _parse_line(line: bytes) -> dict | None:
    text = line.decode("ascii", errors="replace")
    field, sep, value = text.partition("=")
    if not sep or not field:
        return None
    result = {"field": field, "value": value}
    if field == "R" and value[:1].isdigit():
        # Confirmed port digit; the rest of the value is an unconfirmed
        # opaque suffix (see module docstring) — kept as raw text, not parsed further.
        result["port"] = int(value[0])
    return result


if __name__ == "__main__":
    assert build_command("D") == b"\x01\x01D\r"
    assert build_command("R") == b"\x01\x01R\r"
    assert select_port_command(1) == b"\x01\x02W1\r"
    assert select_port_command(8) == b"\x01\x02W8\r"
    try:
        select_port_command(9)
        assert False, "expected ValueError for an out-of-range port"
    except ValueError:
        pass
    try:
        select_port_command(0)
        assert False, "expected ValueError for port 0"
    except ValueError:
        pass

    assert normalize_port_labels(None) == [""] * 8
    assert normalize_port_labels(["20m Dipole", "Vertical"]) == ["20m Dipole", "Vertical", "", "", "", "", "", ""]
    assert normalize_port_labels(["1", "2", "3", "4", "5", "6", "7", "8", "9 (extra, dropped)"]) == \
        ["1", "2", "3", "4", "5", "6", "7", "8"]

    # Exact bytes from the real capture (server/serial_sniffer.py MITM
    # log, RSW8A1ER, all 8 ports exercised in sequence).
    parser = ReplyParser()
    assert parser.feed(b"\x44\x3d\x38\x41\x31\x52\x0d\x0a\x3e") == [{"field": "D", "value": "8A1R"}]
    assert parser.feed(b"\x52\x3d\x36\x36\x31\x30\x0d\x0a\x3e") == [{"field": "R", "value": "6610", "port": 6}]

    for port in range(1, 9):
        reply = f"R={port}{port}10\r\n>".encode("ascii")
        assert parser.feed(reply) == [{"field": "R", "value": f"{port}{port}10", "port": port}]

    # split across two feed() calls, as a real serial read would arrive
    parser2 = ReplyParser()
    assert parser2.feed(b"R=1110\r") == []
    assert parser2.feed(b"\n>") == [{"field": "R", "value": "1110", "port": 1}]

    # garbage/partial data before a real line must not raise or get stuck
    parser3 = ReplyParser()
    assert parser3.feed(b"garbage\r\nR=2210\r\n>") == [{"field": "R", "value": "2210", "port": 2}]

    print("rsw8a1er_protocol.py: ok")
