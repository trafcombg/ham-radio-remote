"""Fixed per-radio port ranges, sized for MAX_RADIOS — see README.md
("Портове и QoS") for the rationale. Radio N (0-based) gets BASE + N*STEP
on each channel; TCP channels (CAT/control) use consecutive ports, the
UDP channels (audio/CW — both latency-sensitive) keep the step-2 spacing
already used in server/config.json/server/db.py's defaults.
"""

MAX_RADIOS = 10

CAT_TCP_PORT_BASE, CAT_TCP_PORT_STEP = 4532, 1       # CI-V/CAT — TCP
CONTROL_PORT_BASE, CONTROL_PORT_STEP = 4632, 1       # control/session — TCP
AUDIO_UDP_PORT_BASE, AUDIO_UDP_PORT_STEP = 5004, 2   # voice audio — UDP, low latency
CW_UDP_PORT_BASE, CW_UDP_PORT_STEP = 5104, 2         # CW keying — UDP, low latency


def port_range(base: int, step: int) -> list[int]:
    return [base + i * step for i in range(MAX_RADIOS)]


def next_free_port(base: int, step: int, used: set[int]) -> int | None:
    for p in port_range(base, step):
        if p not in used:
            return p
    return None  # all MAX_RADIOS slots taken


if __name__ == "__main__":
    assert port_range(4532, 1) == list(range(4532, 4542))
    assert port_range(5004, 2) == [5004 + 2 * i for i in range(10)]

    assert next_free_port(4532, 1, set()) == 4532
    assert next_free_port(4532, 1, {4532, 4533}) == 4534
    assert next_free_port(4532, 1, set(range(4532, 4542))) is None  # all 10 taken

    print("port_ranges.py: ok")
