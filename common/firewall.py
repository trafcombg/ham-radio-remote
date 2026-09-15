"""Windows Firewall inbound-rule check/open for this app's TCP/UDP ports
— netsh advfirewall, no new dependency, Windows only.

Checking is read-only (no elevation needed). Opening missing rules
elevates ONLY netsh.exe via ShellExecute "runas" (one UAC prompt for all
missing rules at once), not the whole app — if the operator declines,
the app keeps running, just logs a warning; nothing here is required for
the app to function, only for other machines to reach it over the LAN.

ponytail: matches rules by exact name-by-convention, not by inspecting
the actual port/protocol/direction of arbitrary existing rules — good
enough to detect "did we already add this," not to audit unrelated ones.
"""

import ctypes
import logging
import subprocess

log = logging.getLogger("firewall")

RULE_PREFIX = "HAM Radio Remote"


def _rule_name(port: int, proto: str) -> str:
    return f"{RULE_PREFIX} {proto.upper()} {port}"


def _rule_exists(port: int, proto: str) -> bool:
    name = _rule_name(port, proto)
    result = subprocess.run(
        ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and "No rules match" not in result.stdout


def missing_rules(ports: list) -> list:
    """ports: [(port, "tcp"|"udp"), ...]. Returns the subset without an
    existing inbound-allow rule."""
    return [(port, proto) for port, proto in ports if not _rule_exists(port, proto)]


def open_ports(missing: list, app_label: str) -> bool:
    """One elevated netsh.exe invocation (one UAC prompt) adding every
    missing rule. Returns whether the elevated command was launched — not
    whether each rule actually got added (that's re-checked next startup
    via missing_rules); a declined UAC prompt makes ShellExecuteW itself
    report failure."""
    if not missing:
        return True
    commands = " && ".join(
        f'netsh advfirewall firewall add rule name="{_rule_name(port, proto)}" '
        f'dir=in action=allow protocol={proto.upper()} localport={port}'
        for port, proto in missing
    )
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c "{commands}"', None, 0)
    if result <= 32:  # ShellExecuteW: > 32 means success, per Win32 docs
        log.warning(
            "%s: не успях да отворя %d firewall правило(а) (UAC отказан или грешка) — "
            "ако има проблеми с връзката отдалечено, отвори ги ръчно в Windows Defender Firewall.",
            app_label, len(missing),
        )
        return False
    log.info("%s: изпратена заявка за отваряне на %d firewall правило(а) — потвърди UAC prompt-а", app_label, len(missing))
    return True


def ensure_ports_open(ports: list, app_label: str):
    """Call once at startup. Never raises — a firewall check failing
    shouldn't block the app itself from starting."""
    try:
        missing = missing_rules(ports)
    except Exception:
        log.exception("%s: firewall проверка се провали — пропускам", app_label)
        return
    if not missing:
        log.info("%s: всички нужни firewall правила вече съществуват", app_label)
        return
    open_ports(missing, app_label)


if __name__ == "__main__":
    assert _rule_name(4632, "tcp") == "HAM Radio Remote TCP 4632"
    assert _rule_name(5004, "udp") == "HAM Radio Remote UDP 5004"
    # _rule_exists/open_ports touch the real Windows Firewall and an
    # elevation prompt — not safe to exercise unattended here; verify
    # manually (run the app, check Windows Defender Firewall rules).
    print("firewall.py: ok")
