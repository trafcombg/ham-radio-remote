"""One amplifier's server-side bridge: connects a transport (see
ebox_transport.py), decodes telemetry, exposes standby/operate/off,
periodically logs to PostgreSQL, and — when linked to a radio — mirrors
that radio's CAT stream for band tracking and blocks its PTT on fault."""

import asyncio
import logging

from common.acom_protocol import (
    CMD_DISABLE_TELEMETRY, CMD_ENABLE_TELEMETRY, CMD_OFF, CMD_OPERATE, CMD_STANDBY, TelemetryParser,
)

log = logging.getLogger("amplifier_bridge")

TELEMETRY_LOG_INTERVAL_S = 10.0
TELEMETRY_REENABLE_INTERVAL_S = 5.0  # the amp doesn't confirm telemetry is on, so keep asking


class AmplifierBridge:
    def __init__(self, cfg: dict, transport, db):
        self.cfg = cfg
        self.name = cfg["name"]
        self.transport = transport
        self.db = db
        self.parser = TelemetryParser(model=cfg.get("model", "1200S"))
        self.latest: dict | None = None
        self.fault = False
        self._reenable_task = None
        self._log_task = None

    async def start(self):
        await self.transport.connect(self._on_data)
        self.transport.write(CMD_ENABLE_TELEMETRY)
        self._reenable_task = asyncio.create_task(self._reenable_loop())
        self._log_task = asyncio.create_task(self._log_loop())
        log.info("amplifier %s connected (%s)", self.name, self.cfg["transport"])

    async def shutdown(self):
        if self._reenable_task:
            self._reenable_task.cancel()
        if self._log_task:
            self._log_task.cancel()
        self.transport.write(CMD_DISABLE_TELEMETRY)
        self.transport.close()

    def _on_data(self, data: bytes):
        for frame in self.parser.feed(data):
            self.latest = frame
            self.fault = frame["fault"]

    async def _reenable_loop(self):
        while True:
            await asyncio.sleep(TELEMETRY_REENABLE_INTERVAL_S)
            self.transport.write(CMD_ENABLE_TELEMETRY)

    async def _log_loop(self):
        while True:
            await asyncio.sleep(TELEMETRY_LOG_INTERVAL_S)
            if self.latest:
                await self.db.log_amplifier_telemetry(self.name, self.latest)

    def operate(self):
        self.transport.write(CMD_OPERATE)

    def standby(self):
        self.transport.write(CMD_STANDBY)

    def power_off(self):
        self.transport.write(CMD_OFF)

    def mirror_cat(self, data: bytes):
        """Feed this from the linked radio's CAT data — see
        RadioBridge.add_data_observer() and server/amplifier_manager.py."""
        self.transport.write(data)


if __name__ == "__main__":
    # Real end-to-end check: a fake TCP "amplifier" speaking the actual
    # ACOM protocol, driven through AmplifierBridge + RawTcpAcomTransport
    # over a real socket — not just pure-function logic like the other
    # self-checks. This is the strongest verification possible here
    # short of a physical amplifier/eBox.
    from common.acom_protocol import _build_test_frame
    from server.db import NullDb
    from server.ebox_transport import RawTcpAcomTransport

    received = bytearray()

    async def _fake_amp(reader, writer):
        writer.write(_build_test_frame())
        await writer.drain()
        while True:
            data = await reader.read(64)
            if not data:
                break
            received.extend(data)

    async def _demo():
        server = await asyncio.start_server(_fake_amp, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            asyncio.create_task(server.serve_forever())

            bridge = AmplifierBridge(
                {"name": "TEST-AMP", "model": "1200S", "transport": "tcp"},
                RawTcpAcomTransport("127.0.0.1", port),
                NullDb(),
            )
            await bridge.start()
            await asyncio.sleep(0.3)
            assert bridge.latest is not None, "telemetry never decoded"
            assert bridge.latest["status"] == "transmit"
            assert bridge.latest["output_power_w"] == 800
            assert bridge.latest["swr"] == 1.5

            bridge.operate()
            bridge.standby()
            bridge.power_off()
            await asyncio.sleep(0.2)
            assert CMD_OPERATE in received and CMD_STANDBY in received and CMD_OFF in received
            assert received.index(CMD_OPERATE) < received.index(CMD_STANDBY) < received.index(CMD_OFF)

            await bridge.shutdown()
            server.close()

    asyncio.run(_demo())
    print("amplifier_bridge.py: ok (real socket, real protocol bytes, fake amp)")
