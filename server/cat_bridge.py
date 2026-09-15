"""CAT (CI-V) serial <-> TCP relay. One of these per radio; PTT arbitration
lives one level up in radio_bridge.py, not here."""

import asyncio
import logging

import serial_asyncio

log = logging.getLogger("cat_bridge")

CIV_GET_FREQUENCY = b"\xfe\xfe\x00\xe0\x03\xfd"


class SerialRelay(asyncio.Protocol):
    def __init__(self):
        self.transport = None
        self.tcp_writer = None
        self.on_data = None  # optional extra hook — used by the admin Test/Verify probe

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        if self.tcp_writer:
            self.tcp_writer.write(data)
        if self.on_data:
            self.on_data(data)

    def connection_lost(self, exc):
        log.warning("serial connection lost: %s", exc)


async def open_serial(serial_port: str, baud: int) -> SerialRelay:
    loop = asyncio.get_running_loop()
    transport, proto = await serial_asyncio.create_serial_connection(loop, SerialRelay, serial_port, baudrate=baud)
    # ponytail: pyserial/Windows assert RTS+DTR high the instant the port
    # opens — for an RTS/DTR-keyed PTT interface that keys the transmitter
    # on every radio add/reload unless cleared right here.
    transport.serial.rts = False
    transport.serial.dtr = False
    return proto


async def make_cat_server(serial_proto: SerialRelay, tcp_host: str, tcp_port: int) -> asyncio.Server:
    """Creates and binds the CAT TCP server; caller runs serve_forever()
    and can later call .close() on the returned server to shut it down."""

    async def handle_client(reader, writer):
        peer = writer.get_extra_info("peername")
        log.info("CAT client connected: %s", peer)
        serial_proto.tcp_writer = writer
        try:
            while True:
                data = await reader.read(256)
                if not data:
                    break
                serial_proto.transport.write(data)
        except ConnectionResetError:
            pass
        finally:
            log.info("CAT client disconnected: %s", peer)
            if serial_proto.tcp_writer is writer:
                serial_proto.tcp_writer = None
            writer.close()

    server = await asyncio.start_server(handle_client, tcp_host, tcp_port)
    log.info("CAT bridge listening on %s:%s", tcp_host, tcp_port)
    return server


async def probe_serial_port(port: str, baud: int, timeout: float = 1.0) -> bool:
    """Opens `port` briefly, asks for the operating frequency, and reports
    whether anything answered — the admin panel's Test/Verify check."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    class ProbeProtocol(asyncio.Protocol):
        def connection_made(self, transport):
            transport.write(CIV_GET_FREQUENCY)

        def data_received(self, data):
            if not fut.done():
                fut.set_result(True)

    transport, _ = await serial_asyncio.create_serial_connection(loop, ProbeProtocol, port, baudrate=baud)
    transport.serial.rts = False
    transport.serial.dtr = False
    try:
        return await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return False
    finally:
        transport.close()
