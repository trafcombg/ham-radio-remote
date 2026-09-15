"""Wraps com0com's setupc.exe to create/list virtual COM port pairs — one
side exposed to local CAT software (WSJT-X, N1MM+, fldigi, ...), the other
used internally by ComRelay to bridge that traffic to the server.

ponytail: the setupc.exe "install"/"list" CLI syntax below follows com0com's
own documented command-line interface, but com0com isn't installed on the
machine this was written on — it hasn't been exercised against a real
installed driver. Verify on a machine with com0com before relying on it;
if the syntax has drifted, `create_pair`'s subprocess call is the only
place that needs fixing.
"""

import logging
import re
import subprocess
import winreg
from pathlib import Path

import serial.tools.list_ports

log = logging.getLogger("com0com")

_PAIR_LINE_RE = re.compile(r"CNC([AB])(\d+)\s+PortName=([^,\s]+)")
_COM_NUM_RE = re.compile(r"COM(\d+)$", re.IGNORECASE)
FIRST_VIRTUAL_COM = 20  # stay well clear of real hardware COM ports


class Com0comNotFound(RuntimeError):
    pass


def _registry_install_dir() -> Path | None:
    roots = [
        winreg.HKEY_LOCAL_MACHINE,
    ]
    subkeys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ]
    for hive in roots:
        for base in subkeys:
            try:
                with winreg.OpenKey(hive, base) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sk:
                                name = winreg.QueryValueEx(sk, "DisplayName")[0]
                                if "com0com" not in name.lower():
                                    continue
                                loc = winreg.QueryValueEx(sk, "InstallLocation")[0]
                                if loc:
                                    return Path(loc)
                        except (OSError, FileNotFoundError):
                            continue
            except (OSError, FileNotFoundError):
                continue
    return None


def find_setupc() -> Path:
    for candidate in (
        Path(r"C:\Program Files (x86)\com0com\setupc.exe"),
        Path(r"C:\Program Files\com0com\setupc.exe"),
    ):
        if candidate.exists():
            return candidate
    install_dir = _registry_install_dir()
    if install_dir and (install_dir / "setupc.exe").exists():
        return install_dir / "setupc.exe"
    raise Com0comNotFound(
        "com0com не е инсталиран (или не е намерен) — виж "
        "https://com0com.sourceforge.net. Без него радиата няма да получат "
        "локални виртуални COM портове."
    )


def _parse_list_output(text: str) -> dict[int, dict[str, str]]:
    """{pair_index: {"A": "COM11", "B": "COM12"}}"""
    pairs: dict[int, dict[str, str]] = {}
    for line in text.splitlines():
        m = _PAIR_LINE_RE.search(line)
        if m:
            side, idx, port = m.group(1), int(m.group(2)), m.group(3)
            pairs.setdefault(idx, {})[side] = port
    return pairs


def list_pairs(setupc: Path) -> dict[int, dict[str, str]]:
    # cwd=setupc.parent: setupc.exe looks up com0com.inf next to itself
    # using a path relative to the CALLER's cwd, not its own — run it from
    # anywhere else and it can't find the driver .inf and pops up a native
    # "SetupOpenInfFile ... ERROR 2" dialog instead of failing cleanly.
    result = subprocess.run(
        [str(setupc), "list"], capture_output=True, text=True, timeout=10, cwd=str(setupc.parent),
    )
    return _parse_list_output(result.stdout)


def _used_com_numbers(pairs: dict[int, dict[str, str]]) -> set[int]:
    used = set()
    for p in serial.tools.list_ports.comports():
        m = _COM_NUM_RE.search(p.device)
        if m:
            used.add(int(m.group(1)))
    for sides in pairs.values():
        for name in sides.values():
            m = _COM_NUM_RE.search(name)
            if m:
                used.add(int(m.group(1)))
    return used


def create_pair(setupc: Path) -> tuple[str, str]:
    """Creates a new com0com pair, returns (exposed_port, internal_port)."""
    used = _used_com_numbers(list_pairs(setupc))
    n = FIRST_VIRTUAL_COM
    while n in used or (n + 1) in used:
        n += 1
    exposed, internal = f"COM{n}", f"COM{n + 1}"
    result = subprocess.run(
        [str(setupc), "install", f"PortName={exposed}", f"PortName={internal}"],
        capture_output=True, text=True, timeout=15, cwd=str(setupc.parent),
    )
    if result.returncode != 0:
        raise RuntimeError(f"com0com install се провали: {(result.stderr or result.stdout).strip()}")
    log.info("created com0com pair %s <-> %s", exposed, internal)
    return exposed, internal


if __name__ == "__main__":
    sample = (
        "CNCA0 PortName=COM11,EmuBR=yes\n"
        "CNCB0 PortName=COM12,EmuBR=yes\n"
        "CNCA1 PortName=COM20,-\n"
        "CNCB1 PortName=COM21,-\n"
    )
    parsed = _parse_list_output(sample)
    assert parsed == {0: {"A": "COM11", "B": "COM12"}, 1: {"A": "COM20", "B": "COM21"}}, parsed
    used = _used_com_numbers(parsed)
    assert {11, 12, 20, 21} <= used, used
    assert _parse_list_output("") == {}
    print("com0com.py: ok")
