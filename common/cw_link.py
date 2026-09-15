"""CW key-event transport: a dedicated low-latency UDP channel, separate
from audio/CAT so key timing never queues behind either. Each event
carries the sending username so the server can gate physical keying
through the same PTT lock used for voice (see server/radio_bridge.py)."""

import json
import logging
import socket
import time

log = logging.getLogger("cw_link")


class CwLink:
    def __init__(self, listen_port: int, peer=None, username: str | None = None):
        self.peer = peer
        self.username = username
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("0.0.0.0", listen_port))

    def send_key(self, on: bool):
        if not self.peer:
            return
        msg = json.dumps({"user": self.username, "on": on, "t": time.monotonic()}).encode()
        try:
            self.sock.sendto(msg, self.peer)
        except OSError:
            log.exception("CW key send failed")

    def poll(self):
        """Non-blocking: returns [(username, on), ...] received since the
        last poll — call this from a socket-readable callback, not a loop."""
        events = []
        while True:
            try:
                data, addr = self.sock.recvfrom(256)
            except BlockingIOError:
                break
            if self.peer is None:
                self.peer = addr
            try:
                msg = json.loads(data)
                events.append((msg["user"], msg["on"]))
            except (json.JSONDecodeError, KeyError):
                pass
        return events

    def close(self):
        self.sock.close()
