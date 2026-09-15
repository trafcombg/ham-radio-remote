"""Stable device identification for Windows: match serial/audio devices by
VID/PID + USB serial number + USB hardware path + WASAPI endpoint id — NOT
by COM port name or PortAudio index, both of which shift across reboots
and replugs. On ambiguity, callers must refuse to start that radio's
bridge rather than guess.
"""

import asyncio
import logging
from dataclasses import dataclass

log = logging.getLogger("device_registry")


@dataclass(frozen=True)
class SerialDeviceInfo:
    port: str  # e.g. "COM5" — resolved fresh on every scan, not stable itself
    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None  # USB hardware path, e.g. "1-4"


@dataclass(frozen=True)
class AudioDeviceInfo:
    index: int  # PortAudio index — resolved fresh on every scan, not stable itself
    name: str
    endpoint_id: str | None  # WASAPI endpoint id, the stable Windows identifier


class DeviceNotFoundError(RuntimeError):
    pass


class AmbiguousDeviceError(RuntimeError):
    pass


def scan_serial_devices() -> list[SerialDeviceInfo]:
    from serial.tools import list_ports

    return [
        SerialDeviceInfo(p.device, p.vid, p.pid, p.serial_number, p.location)
        for p in list_ports.comports()
    ]


def scan_audio_devices() -> list[AudioDeviceInfo]:
    import sounddevice as sd

    devices = list(sd.query_devices())
    ids = _pair_endpoint_ids([d["name"] for d in devices], _wasapi_endpoint_ids())
    return [AudioDeviceInfo(i, d["name"], eid) for i, (d, eid) in enumerate(zip(devices, ids))]


def _pair_endpoint_ids(names: list[str], ids_by_name: dict) -> list:
    """Match each PortAudio device name to a pycaw endpoint id, consuming
    ids in order per name so devices sharing a friendly name (e.g. a stereo
    USB codec enumerated as two identical "Microphone" entries) get distinct
    ids instead of all collapsing onto the same one."""
    used: dict[str, int] = {}
    result = []
    for name in names:
        pos = used.get(name, 0)
        ids = ids_by_name.get(name, [])
        result.append(ids[pos] if pos < len(ids) else None)
        used[name] = pos + 1
    return result


def _wasapi_endpoint_ids() -> dict:
    """name -> list of WASAPI endpoint ids (one per device with that name),
    for every device pycaw can see. Queried once per scan, not once per
    device: pycaw's own GetAllDevices() prints a UserWarning per endpoint
    it can't fully query (common for disabled or disconnected devices
    Windows still lists) — calling it once instead of per-device avoids
    both repeating that noise N times per scan and N redundant COM
    enumerations. The warning itself is harmless (this function degrades
    to skipping that device's endpoint id) so it's suppressed rather than
    left to spam the console."""
    try:
        import warnings

        from pycaw.pycaw import AudioUtilities

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            devices = AudioUtilities.GetAllDevices()
        ids_by_name: dict[str, list[str]] = {}
        for dev in devices:
            ids_by_name.setdefault(dev.FriendlyName, []).append(dev.id)
        return ids_by_name
    except Exception:
        log.debug("WASAPI endpoint id lookup unavailable", exc_info=True)
        return {}


def resolve_serial_port(devices, *, vid=None, pid=None, serial_number=None, location=None) -> str:
    matches = [
        d for d in devices
        if (vid is None or d.vid == vid)
        and (pid is None or d.pid == pid)
        and (serial_number is None or d.serial_number == serial_number)
        and (location is None or d.location == location)
    ]
    if not matches:
        raise DeviceNotFoundError(
            f"no serial device matches vid={vid} pid={pid} serial={serial_number} location={location}"
        )
    if len(matches) > 1:
        raise AmbiguousDeviceError(
            f"{len(matches)} serial devices match vid={vid} pid={pid} serial={serial_number} "
            f"location={location} -> {[d.port for d in matches]}; add serial_number/location to disambiguate"
        )
    return matches[0].port


def resolve_audio_device(devices, *, endpoint_id=None, name_contains=None) -> int:
    matches = [
        d for d in devices
        if (endpoint_id is None or d.endpoint_id == endpoint_id)
        and (name_contains is None or name_contains.lower() in d.name.lower())
    ]
    if not matches:
        raise DeviceNotFoundError(f"no audio device matches endpoint_id={endpoint_id} name_contains={name_contains}")
    if len(matches) > 1:
        raise AmbiguousDeviceError(
            f"{len(matches)} audio devices match endpoint_id={endpoint_id} name_contains={name_contains} "
            f"-> {[d.name for d in matches]}; add endpoint_id to disambiguate"
        )
    return matches[0].index


# ponytail: polling, not a WM_DEVICECHANGE hook — good enough to notice a
# radio interface vanish/appear within `interval_s`. Swap for a real
# device-change message pump if sub-second detection is ever needed.
async def watch_devices(interval_s: float):
    last_serial = {d.port for d in scan_serial_devices()}
    last_audio = {d.name for d in scan_audio_devices()}
    while True:
        await asyncio.sleep(interval_s)
        serial_now = {d.port for d in scan_serial_devices()}
        audio_now = {d.name for d in scan_audio_devices()}
        if serial_now != last_serial:
            log.info("serial devices changed: +%s -%s", serial_now - last_serial, last_serial - serial_now)
            last_serial = serial_now
        if audio_now != last_audio:
            log.info("audio devices changed: +%s -%s", audio_now - last_audio, last_audio - audio_now)
            last_audio = audio_now


if __name__ == "__main__":
    devices = [
        SerialDeviceInfo("COM5", 0x10C4, 0xEA60, "AB123", "1-4"),
        SerialDeviceInfo("COM6", 0x0403, 0x6001, "CD456", "1-5"),
        SerialDeviceInfo("COM7", 0x10C4, 0xEA60, "EF789", "1-6"),  # same vid/pid as COM5
    ]
    assert resolve_serial_port(devices, vid=0x0403, pid=0x6001) == "COM6"
    try:
        resolve_serial_port(devices, vid=0x10C4, pid=0xEA60)
        assert False, "expected AmbiguousDeviceError"
    except AmbiguousDeviceError:
        pass
    assert resolve_serial_port(devices, vid=0x10C4, pid=0xEA60, serial_number="AB123") == "COM5"
    try:
        resolve_serial_port(devices, vid=0x9999, pid=0x9999)
        assert False, "expected DeviceNotFoundError"
    except DeviceNotFoundError:
        pass

    # duplicate-named audio devices (e.g. two "Microphone" endpoints) must
    # get distinct ids, not both collapse onto the same one
    names = ["Microphone (2- USB Audio CODEC )", "Microphone (2- USB Audio CODEC )", "Speakers (Realtek)"]
    ids_by_name = {
        "Microphone (2- USB Audio CODEC )": ["{guid-mic-1}", "{guid-mic-2}"],
        "Speakers (Realtek)": ["{guid-spk}"],
    }
    assert _pair_endpoint_ids(names, ids_by_name) == ["{guid-mic-1}", "{guid-mic-2}", "{guid-spk}"]
    assert _pair_endpoint_ids(["Unknown Device"], {}) == [None]

    print("device_registry.py: ok")
