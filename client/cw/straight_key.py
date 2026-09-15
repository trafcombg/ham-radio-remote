"""Straight key: the operator's on/off state IS the key state — no timing
logic needed, just forward it."""


class StraightKeySource:
    def __init__(self, on_key):
        self.on_key = on_key

    def press(self):
        self.on_key(True)

    def release(self):
        self.on_key(False)
