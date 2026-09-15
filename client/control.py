"""Control connection: login + PTT request/response + busy status. Kept
separate from the raw CAT byte tunnel so the server can arbitrate PTT
before it reaches the radio, regardless of PTT method (CI-V or RTS/DTR)."""

import asyncio
import json
import logging

from common.control_protocol import KEEPALIVE_INTERVAL_S, READ_TIMEOUT_S

log = logging.getLogger("control")


class ControlClient:
    def __init__(self, username: str, password: str, server_host: str, server_port: int):
        self.username = username
        self.password = password
        self.server_host = server_host
        self.server_port = server_port
        self.writer = None
        self.busy_by = None  # username currently holding PTT on this radio, or None
        self.last_notice = None  # one-shot message for the UI, e.g. admin reconfigured the radio
        self.denied = False  # true once the server rejects hello (bad password / no permission)
        self.on_reconfigured = None  # optional callable() — e.g. session.py rebuilding AudioLink with new settings

    async def run(self):
        reader, writer = await asyncio.open_connection(self.server_host, self.server_port)
        self.writer = writer
        await self._send({"type": "hello", "username": self.username, "password": self.password})
        ping_task = asyncio.create_task(self._ping_loop())
        try:
            while True:
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    # Server went silent (should be sending its own
                    # keepalive pings) — a silent network drop, not a
                    # clean close, never trips "if not line: break" on
                    # its own. Treat it as dead so the reconnect loop in
                    # session.py takes over instead of hanging here
                    # forever — this is exactly what left PTT stuck.
                    log.warning("control server %s:%s went silent — reconnecting", self.server_host, self.server_port)
                    break
                if not line:
                    break
                msg = json.loads(line)
                if msg["type"] == "status":
                    self.busy_by = msg["busy_by"]
                elif msg["type"] == "ptt_denied":
                    log.warning("PTT denied: %s", msg["reason"])
                elif msg["type"] == "hello_denied":
                    self.denied = True
                    self.last_notice = f"Достъп отказан: {msg.get('reason', 'грешни данни')}"
                    log.warning(self.last_notice)
                    break
                elif msg["type"] == "reconfigured":
                    self.last_notice = "Радиото беше преконфигурирано от администратор — връзката се затваря"
                    log.warning(self.last_notice)
                    if self.on_reconfigured:
                        self.on_reconfigured()
                    break
                elif msg["type"] == "ping":
                    pass  # server's keepalive — just proof of life
        finally:
            ping_task.cancel()
            writer.close()
            self.writer = None

    async def _ping_loop(self):
        """Gives the server's own read timeout something to see from us
        during a quiet stretch (no PTT activity) — otherwise a healthy
        but idle client would eventually look indistinguishable from a
        dead one from the server's side."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL_S)
            await self._send({"type": "ping"})

    async def request_ptt(self, on: bool):
        await self._send({"type": "ptt", "on": on})

    async def _send(self, obj):
        if self.writer:
            self.writer.write((json.dumps(obj) + "\n").encode())
            await self.writer.drain()


if __name__ == "__main__":
    async def _demo_reconfigured_fires_callback_and_breaks():
        # A radio config change (e.g. codec/sample_rate from the admin
        # panel) sends "reconfigured" then closes — run() must invoke the
        # callback AND return promptly, not keep waiting on the socket.
        async def handle(reader, writer):
            await reader.readline()  # hello
            writer.write(b'{"type": "reconfigured"}\n')
            await writer.drain()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            client = ControlClient("ivan", "secret", "127.0.0.1", port)
            fired = []
            client.on_reconfigured = lambda: fired.append(True)
            await asyncio.wait_for(client.run(), timeout=2.0)
            assert fired == [True]

    asyncio.run(_demo_reconfigured_fires_callback_and_breaks())
    print("control.py: ok")
