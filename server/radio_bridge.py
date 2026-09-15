"""One radio's full server-side bridge: CAT relay, control channel (login +
PTT arbitration + status broadcast), the audio link, and PTT keying itself.

PTT is deliberately NOT sniffed out of the raw CAT byte stream: RTS/DTR
keying isn't CAT data at all (it's a serial control line), and arbitration
has to happen before the physical action, not after. So PTT is its own
small JSON-lines control connection, separate from the CAT passthrough
that third-party software (WSJT-X etc.) uses transparently.

shutdown() lets the admin panel (Phase 3) stop/replace a single radio
without restarting the server.
"""

import asyncio
import json
import logging

import serial

from common.audio_io import AudioLink
from common.civ import ptt_command
from server.cat_bridge import CIV_GET_FREQUENCY, make_cat_server, open_serial
from server.ptt_arbiter import PttArbiter, PttDenied

log = logging.getLogger("radio_bridge")


class CivPtt:
    def __init__(self, transport, civ_address: int):
        self._transport = transport
        self._addr = civ_address

    def set(self, on: bool):
        self._transport.write(ptt_command(self._addr, on))


class LinePtt:
    """Keys PTT via the RTS or DTR line of a serial port (microHAM-style)."""

    def __init__(self, pyserial_conn, line: str):
        self._conn = pyserial_conn
        self._line = line

    def set(self, on: bool):
        setattr(self._conn, self._line, on)


class RadioBridge:
    def __init__(self, cfg: dict, db):
        self.cfg = cfg
        self.name = cfg["name"]
        self.db = db
        self.arbiter = PttArbiter()
        self.control_clients: dict = {}  # writer -> username
        self.session_ids: dict = {}      # writer -> db session id
        self.tx_ids: dict = {}           # writer -> current db transmission id

        self.serial_proto = None
        self.audio = None
        self.ptt_method = None
        self._cat_server = None
        self._control_server = None
        self._extra_ptt_serial = None  # only set if PTT uses a distinct serial port
        self._test_future = None

    async def start(self):
        self.serial_proto = await open_serial(self.cfg["cat"]["serial_port"], self.cfg["cat"]["baud"])
        self.serial_proto.on_data = self._on_serial_data
        self.ptt_method = self._build_ptt_method()

        audio_cfg = self.cfg["audio"]
        self.audio = AudioLink(
            input_device=audio_cfg.get("input_device"),
            output_device=audio_cfg.get("output_device"),
            listen_port=audio_cfg["udp_port"],
        )
        self.audio.start()

        host = self.cfg["cat"].get("tcp_host", "0.0.0.0")
        self._cat_server = await make_cat_server(self.serial_proto, host, self.cfg["cat"]["tcp_port"])
        self._control_server = await asyncio.start_server(self._handle_control_client, host, self.cfg["control_port"])
        log.info(
            "radio %s up: CAT :%s control :%s audio UDP :%s",
            self.name, self.cfg["cat"]["tcp_port"], self.cfg["control_port"], audio_cfg["udp_port"],
        )
        try:
            await asyncio.gather(self._cat_server.serve_forever(), self._control_server.serve_forever())
        except asyncio.CancelledError:
            pass

    async def shutdown(self):
        await self._broadcast({"type": "reconfigured"})
        if self._control_server:
            self._control_server.close()
        if self._cat_server:
            self._cat_server.close()
        if self.audio:
            self.audio.stop()
        if self._extra_ptt_serial:
            self._extra_ptt_serial.close()
        if self.serial_proto and self.serial_proto.transport:
            self.serial_proto.transport.close()
        for w in list(self.control_clients):
            w.close()

    def _on_serial_data(self, data: bytes):
        if self._test_future and not self._test_future.done():
            self._test_future.set_result(True)

    async def test_cat(self, timeout: float = 1.0) -> bool:
        """Active CAT probe on the port this bridge already has open —
        used instead of opening a second handle to the same COM port."""
        if not self.serial_proto or not self.serial_proto.transport:
            return False
        loop = asyncio.get_running_loop()
        self._test_future = loop.create_future()
        self.serial_proto.transport.write(CIV_GET_FREQUENCY)
        try:
            return await asyncio.wait_for(self._test_future, timeout)
        except asyncio.TimeoutError:
            return False
        finally:
            self._test_future = None

    def _build_ptt_method(self):
        ptt_cfg = self.cfg["ptt"]
        method = ptt_cfg["method"]
        if method == "civ":
            return CivPtt(self.serial_proto.transport, ptt_cfg["civ_address"])
        if method in ("rts", "dtr"):
            ptt_port = ptt_cfg.get("serial_port")
            if ptt_port and ptt_port != self.cfg["cat"]["serial_port"]:
                self._extra_ptt_serial = serial.Serial(ptt_port, ptt_cfg.get("baud", self.cfg["cat"]["baud"]))
                conn = self._extra_ptt_serial
            else:
                conn = self.serial_proto.transport.serial  # reuse the CAT connection's line
            return LinePtt(conn, method)
        raise ValueError(f"unknown PTT method for {self.name}: {method}")

    async def _handle_control_client(self, reader, writer):
        username = None
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = json.loads(line)
                if msg["type"] == "hello":
                    username = msg["username"]
                    self.control_clients[writer] = username
                    self.session_ids[writer] = await self.db.start_session(username, self.name)
                    await self._send(writer, {"type": "hello_ack", "radio": self.name})
                    await self._broadcast_status()
                elif msg["type"] == "ptt" and username:
                    await self._handle_ptt(writer, username, msg["on"])
        except (ConnectionResetError, json.JSONDecodeError):
            pass
        finally:
            if username:
                await self._release_if_holder(writer, username)
                session_id = self.session_ids.pop(writer, None)
                if session_id is not None:
                    await self.db.end_session(session_id)
            self.control_clients.pop(writer, None)
            writer.close()
            await self._broadcast_status()

    async def _handle_ptt(self, writer, username, on):
        if on:
            try:
                self.arbiter.acquire(username)
            except PttDenied as e:
                await self._send(writer, {"type": "ptt_denied", "reason": str(e)})
                return
            self.ptt_method.set(True)
            session_id = self.session_ids.get(writer)
            self.tx_ids[writer] = await self.db.start_transmission(session_id) if session_id is not None else None
            await self._send(writer, {"type": "ptt_ack", "on": True})
        else:
            if self.arbiter.holder != username:
                await self._send(writer, {"type": "ptt_ack", "on": False})
                return
            self.arbiter.release(username)
            self.ptt_method.set(False)
            tx_id = self.tx_ids.pop(writer, None)
            if tx_id is not None:
                await self.db.end_transmission(tx_id)
            await self._send(writer, {"type": "ptt_ack", "on": False})
        await self._broadcast_status()

    async def _release_if_holder(self, writer, username):
        if self.arbiter.holder == username:
            self.arbiter.release(username)
            self.ptt_method.set(False)
            tx_id = self.tx_ids.pop(writer, None)
            if tx_id is not None:
                await self.db.end_transmission(tx_id)

    async def _send(self, writer, obj):
        writer.write((json.dumps(obj) + "\n").encode())
        await writer.drain()

    async def _broadcast(self, obj):
        for w in list(self.control_clients):
            try:
                await self._send(w, obj)
            except (ConnectionError, OSError):
                pass

    async def _broadcast_status(self):
        await self._broadcast({"type": "status", "busy_by": self.arbiter.holder})
