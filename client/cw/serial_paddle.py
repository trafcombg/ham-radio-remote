"""Reads paddle dit/dah contacts from a USB-serial adapter's CTS/DSR input
lines — the common way cheap "poor man's paddle to USB" interfaces work.

ponytail / needs real hardware to verify: exact pin mapping (which
contact is CTS vs DSR, and whether it's active-low) varies by adapter.
CTS=dit / DSR=dah / active-low is the most common convention for these
cables, but confirm against your actual interface — swap dit_pin/dah_pin
or active_low if dit and dah come out backwards or inverted.
"""

import serial


class SerialPaddle:
    def __init__(self, port: str, dit_pin: str = "cts", dah_pin: str = "dsr", active_low: bool = True):
        self._ser = serial.Serial(port)
        self._dit_pin = dit_pin
        self._dah_pin = dah_pin
        self._active_low = active_low

    def _read(self, pin: str) -> bool:
        state = getattr(self._ser, pin)
        return (not state) if self._active_low else state

    @property
    def dit(self) -> bool:
        return self._read(self._dit_pin)

    @property
    def dah(self) -> bool:
        return self._read(self._dah_pin)

    def close(self):
        self._ser.close()
