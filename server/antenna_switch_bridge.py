"""One RSW8A1ER antenna switch's server-side bridge: connects the serial
transport, tracks the currently selected port, exposes select_port(),
and logs each switch to PostgreSQL. Unlike the ACOM amplifier, a switch
is always tied to exactly one radio (it's wired inline with that radio's
feedline, not shared) — access control reuses the radio's own
user_can_access_radio grant (see server/admin_api.py) rather than a
separate per-switch ACL.
"""

import logging

from common.rsw8a1er_protocol import CMD_QUERY_STATE, ReplyParser, select_port_command

log = logging.getLogger("antenna_switch_bridge")


class AntennaSwitchBridge:
    def __init__(self, cfg: dict, transport, db):
        self.cfg = cfg
        self.name = cfg["name"]
        self.transport = transport
        self.db = db
        self.parser = ReplyParser()
        self.port: int | None = None  # currently selected antenna port, learned on connect/after each switch
        self.device_id: str | None = None  # raw "D=" reply, e.g. "8A1R" — informational only

    async def start(self):
        await self.transport.connect(self._on_data)
        self.transport.write(CMD_QUERY_STATE)  # learn the port the switch is already on
        log.info("antenna switch %s connected (%s)", self.name, self.cfg.get("serial_port"))

    async def shutdown(self):
        self.transport.close()

    def _on_data(self, data: bytes):
        for reply in self.parser.feed(data):
            if reply["field"] == "R" and "port" in reply:
                self.port = reply["port"]
            elif reply["field"] == "D":
                self.device_id = reply["value"]

    def query_port(self):
        """Manual re-query (admin panel's "Провери порт" button) — same
        command start() already sends on connect, for when the switch's
        port was changed from its own front panel/another controller and
        the server's cached self.port has drifted out of sync."""
        self.transport.write(CMD_QUERY_STATE)

    async def select_port(self, port: int, username: str | None = None):
        self.transport.write(select_port_command(port))
        # Optimistic — the switch's own R-shaped reply to Wn will confirm
        # (or correct) this via _on_data, same convention as
        # RadioPanelController.set_frequency in the client.
        self.port = port
        await self.db.log_antenna_switch_event(self.name, port, username)


if __name__ == "__main__":
    import asyncio

    from server.db import NullDb

    class _FakeTransport:
        """Loops select_port_command()/CMD_QUERY_STATE straight back
        through the real ReplyParser as the switch's own R-shaped
        confirmation would — exercises the actual wire format, not just
        AntennaSwitchBridge's bookkeeping."""

        def __init__(self):
            self.sent = []
            self.on_data = None

        async def connect(self, on_data):
            self.on_data = on_data

        def write(self, data: bytes):
            self.sent.append(data)
            if data == CMD_QUERY_STATE:
                self.on_data(b"R=6610\r\n>")  # switch was already on port 6
            elif data[2:3] == b"W":  # 0x01 LEN 'W' <digit> CR — see common/rsw8a1er_protocol.py
                port = int(chr(data[3]))
                self.on_data(f"R={port}{port}10\r\n>".encode())

        def close(self):
            pass

    async def _demo():
        bridge = AntennaSwitchBridge({"name": "SW1"}, _FakeTransport(), NullDb())
        await bridge.start()
        assert bridge.port == 6, f"initial port query never applied, got {bridge.port}"

        await bridge.select_port(3, "ivan")
        assert bridge.port == 3
        assert bridge.transport.sent[-1] == select_port_command(3)

        for port in range(1, 9):
            await bridge.select_port(port, "ivan")
            assert bridge.port == port

        bridge.query_port()
        assert bridge.transport.sent[-1] == CMD_QUERY_STATE

    asyncio.run(_demo())
    print("antenna_switch_bridge.py: ok")
