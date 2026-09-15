"""Iambic (Mode A) paddle keyer: converts dit/dah paddle contact state
into timed key-on/off events at a given WPM.

Squeeze (both paddles closed) alternates dit-dah-dit-dah. Releasing a
paddle mid-element does NOT add a trailing opposite element — that's
Mode B's "dot memory"; Mode A is simpler and good enough for a prototype,
add Mode B if a user actually asks for it.

next_element() is the pure decision core (paddle state + last element ->
next element), split out so it's testable without real-time timing.
run() is the thin async wrapper that drives it against a real clock.
"""

import asyncio


def next_element(dit: bool, dah: bool, last_element: str | None) -> str | None:
    if dit and dah:
        return "dah" if last_element == "dit" else "dit"
    if dit:
        return "dit"
    if dah:
        return "dah"
    return None


class IambicKeyer:
    def __init__(self, wpm: int, on_key, paddle):
        """paddle: object with .dit and .dah booleans, updated externally
        (e.g. by client/cw/serial_paddle.py polling real hardware).
        on_key: callable(bool) invoked on every key up/down transition."""
        self.wpm = wpm
        self.on_key = on_key
        self.paddle = paddle
        self._last_element = None
        self._running = False

    @property
    def dit_s(self) -> float:
        return 1.2 / self.wpm

    async def run(self):
        self._running = True
        while self._running:
            elem = next_element(self.paddle.dit, self.paddle.dah, self._last_element)
            if elem is None:
                self._last_element = None
                await asyncio.sleep(self.dit_s / 4)
                continue
            await self._send_element(elem)

    async def _send_element(self, elem: str):
        self._last_element = elem
        duration = self.dit_s if elem == "dit" else 3 * self.dit_s
        self.on_key(True)
        await asyncio.sleep(duration)
        self.on_key(False)
        await asyncio.sleep(self.dit_s)  # inter-element gap

    def stop(self):
        self._running = False


if __name__ == "__main__":
    assert next_element(True, False, None) == "dit"
    assert next_element(False, True, None) == "dah"
    assert next_element(False, False, None) is None
    assert next_element(True, True, None) == "dit"
    assert next_element(True, True, "dit") == "dah"
    assert next_element(True, True, "dah") == "dit"
    assert next_element(True, True, "dah") != "dah"  # never repeats during a squeeze
    print("iambic.py: ok")
