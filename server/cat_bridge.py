"""CAT (CI-V) serial <-> TCP relay. One of these per radio; PTT arbitration
lives one level up in radio_bridge.py, not here."""

import asyncio
import logging

import serial_asyncio

log = logging.getLogger("cat_bridge")


class SerialRelay(asyncio.Protocol):
    def __init__(self):
        self.transport = None
        self.tcp_writer = None

    def connection_made(self, transport):
        self.transport = transport

    def data_received(self, data):
        if self.tcp_writer:
            self.tcp_writer.write(data)

    def connection_lost(self, exc):
        log.warning("serial connection lost: %s", exc)


async def open_serial(serial_port: str, baud: int) -> SerialRelay:
    loop = asyncio.get_running_loop()
    _, proto = await serial_asyncio.create_serial_connection(loop, SerialRelay, serial_port, baudrate=baud)
    return proto


async def serve_cat(serial_proto: SerialRelay, tcp_host: str, tcp_port: int):
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
    async with server:
        await server.serve_forever()
