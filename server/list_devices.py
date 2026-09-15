"""Prints currently attached serial + audio devices with their stable IDs.
Use this to fill in vid/pid/serial_number/endpoint_id in config.json.

Run from the repo root: python -m server.list_devices
"""

from server.device_registry import scan_audio_devices, scan_serial_devices

if __name__ == "__main__":
    print("Serial devices:")
    for d in scan_serial_devices():
        vidpid = f"{d.vid:#06x}:{d.pid:#06x}" if d.vid is not None else "?"
        print(f"  {d.port}  vid:pid={vidpid}  serial={d.serial_number}  location={d.location}")

    print("Audio devices:")
    for d in scan_audio_devices():
        print(f"  [{d.index}] {d.name}  endpoint_id={d.endpoint_id}")
