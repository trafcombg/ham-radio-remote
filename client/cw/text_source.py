"""Text -> Morse: encodes typed text into timed key on/off events and
plays them out asynchronously, cancelling any send still in progress."""

import asyncio

from common.morse import encode_text


class TextCwSource:
    def __init__(self, wpm: int, on_key):
        self.wpm = wpm
        self.on_key = on_key
        self._task = None

    async def send(self, text: str):
        """A coroutine — call via asyncio.run_coroutine_threadsafe() from a
        different thread (e.g. the Qt UI thread), like control.request_ptt()."""
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._play(text))

    async def _play(self, text: str):
        for on, duration_ms in encode_text(text, self.wpm):
            self.on_key(on)
            await asyncio.sleep(duration_ms / 1000)
        self.on_key(False)

    def stop(self):
        if self._task:
            self._task.cancel()
