"""One radio's full server-side bridge: CAT relay, control channel (login +
PTT arbitration + status broadcast), the audio link, PTT keying, and CW
keying.

PTT is deliberately NOT sniffed out of the raw CAT byte stream: RTS/DTR
keying isn't CAT data at all (it's a serial control line), and arbitration
has to happen before the physical action, not after. So PTT is its own
small JSON-lines control connection, separate from the CAT passthrough
that third-party software (WSJT-X etc.) uses transparently.

CW keying reuses the exact same physical actions as PTT (CivKey/LineKey —
for most Icom rigs, CW keying via CAT/key-line IS just the transmit
command/line, same as voice PTT) and the exact same PttArbiter lock: the
first key-down from an idle radio auto-acquires it for that user, a second
user's key events are dropped while someone else holds it, and it's
auto-released after a short CW idle period (nobody sends an explicit
"done" — a paddle only ever tells you it's up).

shutdown() lets the admin panel (Phase 3) stop/replace a single radio
without restarting the server.
"""

import asyncio
import json
import logging

import serial

from common.audio_io import AudioLink
from common.civ import ptt_command
from common.cw_link import CwLink
from server.cat_bridge import CIV_GET_FREQUENCY, make_cat_server, open_serial
from server.ptt_arbiter import PttArbiter, PttDenied

log = logging.getLogger("radio_bridge")

CW_IDLE_RELEASE_S = 1.5  # release the shared lock this long after the last key-up


class CivKey:
    """CI-V transmit command — used for voice PTT and, on rigs with no
    separate CW CAT command, for CW keying too (same physical action)."""

    def __init__(self, transport, civ_address: int):
        self._transport = transport
        self._addr = civ_address

    def set(self, on: bool):
        self._transport.write(ptt_command(self._addr, on))


class LineKey:
    """Keys the RTS or DTR line of a serial port (microHAM-style)."""

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
        self.cw_method = None
        self.cw_link = None
        self._cat_server = None
        self._control_server = None
        self._extra_serials: dict = {}  # port name -> pyserial.Serial, for PTT/CW on a distinct port
        self._test_future = None
        self._cw_release_handle = None

    async def start(self):
        self.serial_proto = await open_serial(self.cfg["cat"]["serial_port"], self.cfg["cat"]["baud"])
        self.serial_proto.on_data = self._on_serial_data
        self.ptt_method = self._build_key_method(self.cfg["ptt"])
        self.cw_method = self._build_key_method(self.cfg.get("cw") or self.cfg["ptt"])

        audio_cfg = self.cfg["audio"]
        self.audio = AudioLink(
            input_device=audio_cfg.get("input_device"),
            output_device=audio_cfg.get("output_device"),
            listen_port=audio_cfg["udp_port"],
        )
        self.audio.start()

        self.cw_link = CwLink(self.cfg["cw_udp_port"])
        asyncio.get_running_loop().add_reader(self.cw_link.sock.fileno(), self._on_cw_readable)

        host = self.cfg["cat"].get("tcp_host", "0.0.0.0")
        self._cat_server = await make_cat_server(self.serial_proto, host, self.cfg["cat"]["tcp_port"])
        self._control_server = await asyncio.start_server(self._handle_control_client, host, self.cfg["control_port"])
        log.info(
            "radio %s up: CAT :%s control :%s audio UDP :%s CW UDP :%s",
            self.name, self.cfg["cat"]["tcp_port"], self.cfg["control_port"],
            audio_cfg["udp_port"], self.cfg["cw_udp_port"],
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
        if self.cw_link:
            asyncio.get_running_loop().remove_reader(self.cw_link.sock.fileno())
            self.cw_link.close()
        if self._cw_release_handle:
            self._cw_release_handle.cancel()
        for conn in self._extra_serials.values():
            conn.close()
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

    def _build_key_method(self, key_cfg: dict):
        method = key_cfg["method"]
        if method == "civ":
            return CivKey(self.serial_proto.transport, key_cfg["civ_address"])
        if method in ("rts", "dtr"):
            port = key_cfg.get("serial_port")
            if port and port != self.cfg["cat"]["serial_port"]:
                if port not in self._extra_serials:
                    self._extra_serials[port] = serial.Serial(port, key_cfg.get("baud", self.cfg["cat"]["baud"]))
                conn = self._extra_serials[port]
            else:
                conn = self.serial_proto.transport.serial  # reuse the CAT connection's line
            return LineKey(conn, method)
        raise ValueError(f"unknown key method for {self.name}: {method}")

    def _on_cw_readable(self):
        for username, on in self.cw_link.poll():
            self._handle_cw_event(username, on)

    def _handle_cw_event(self, username: str, on: bool):
        if self.arbiter.holder is None and on:
            self.arbiter.acquire(username)
        if self.arbiter.holder != username:
            return  # radio held by someone else — drop the key event
        self.cw_method.set(on)
        if self._cw_release_handle:
            self._cw_release_handle.cancel()
            self._cw_release_handle = None
        if not on:
            loop = asyncio.get_running_loop()
            self._cw_release_handle = loop.call_later(CW_IDLE_RELEASE_S, self._cw_release, username)

    def _cw_release(self, username: str):
        self._cw_release_handle = None
        if self.arbiter.holder == username:
            self.arbiter.release(username)
            asyncio.create_task(self._broadcast_status())

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


if __name__ == "__main__":
    # ponytail-required self-check for the CW arbitration branch logic —
    # no real serial/network involved, __init__ does no I/O.
    import asyncio

    from server.db import NullDb

    class _FakeKey:
        def __init__(self):
            self.calls = []

        def set(self, on):
            self.calls.append(on)

    async def _demo():
        bridge = RadioBridge({"name": "TEST"}, NullDb())
        bridge.cw_method = _FakeKey()

        bridge._handle_cw_event("ivan", True)
        assert bridge.arbiter.holder == "ivan"
        assert bridge.cw_method.calls == [True]

        bridge._handle_cw_event("georgi", True)  # dropped — ivan already holds it
        assert bridge.cw_method.calls == [True]
        assert bridge.arbiter.holder == "ivan"

        bridge._handle_cw_event("ivan", False)
        assert bridge.cw_method.calls == [True, False]
        assert bridge.arbiter.holder == "ivan"  # not released yet — idle timer pending

        bridge._cw_release_handle.cancel()
        bridge._cw_release("ivan")  # simulate the idle timeout firing
        assert bridge.arbiter.holder is None

        bridge._handle_cw_event("georgi", True)  # now free — georgi can take it
        assert bridge.arbiter.holder == "georgi"

    asyncio.run(_demo())
    print("radio_bridge.py: ok")
