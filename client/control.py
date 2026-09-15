"""Control connection: login + PTT request/response + busy status. Kept
separate from the raw CAT byte tunnel so the server can arbitrate PTT
before it reaches the radio, regardless of PTT method (CI-V or RTS/DTR)."""

import asyncio
import json
import logging

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

    async def run(self):
        reader, writer = await asyncio.open_connection(self.server_host, self.server_port)
        self.writer = writer
        await self._send({"type": "hello", "username": self.username, "password": self.password})
        try:
            while True:
                line = await reader.readline()
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
        finally:
            writer.close()
            self.writer = None

    async def request_ptt(self, on: bool):
        await self._send({"type": "ptt", "on": on})

    async def _send(self, obj):
        if self.writer:
            self.writer.write((json.dumps(obj) + "\n").encode())
            await self.writer.drain()
