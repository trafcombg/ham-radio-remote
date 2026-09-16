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
import contextlib
import json
import logging

import serial

from common.audio_io import AudioLink
from common.civ import ptt_command
from common.control_protocol import KEEPALIVE_INTERVAL_S as CONTROL_KEEPALIVE_INTERVAL_S
from common.control_protocol import READ_TIMEOUT_S as CONTROL_READ_TIMEOUT_S
from common.cw_link import CwLink
from server.cat_bridge import CIV_GET_FREQUENCY, make_cat_server, open_serial
from server.db import PostgresDb
from server.ptt_arbiter import PttArbiter, PttDenied

log = logging.getLogger("radio_bridge")

CW_IDLE_RELEASE_S = 1.5  # release the shared lock this long after the last key-up


class CivKey:
    """CI-V transmit command — used for voice PTT and, on rigs with no
    separate CW CAT command, for CW keying too (same physical action).

    Reads serial_proto.transport lazily on every set(), not once at
    construction: serial_asyncio schedules Protocol.connection_made via
    loop.call_soon, so the transport can still be None for a moment right
    after open_serial() returns — capturing it once raced that window.
    A later physical disconnect (transport goes back to None — see
    cat_bridge.SerialRelay.connection_lost) is handled the same way."""

    def __init__(self, serial_proto, civ_address: int):
        self._serial_proto = serial_proto
        self._addr = civ_address

    def set(self, on: bool):
        transport = self._serial_proto.transport
        if transport is None:
            log.warning("PTT/CW keyed via CI-V but the serial connection isn't up — dropped")
            return
        transport.write(ptt_command(self._addr, on))


class LineKey:
    """Keys the RTS or DTR line of a serial port (microHAM-style). `conn`
    may be a pyserial.Serial, or a zero-arg callable returning one (or
    None) — used when the line lives on the shared CAT connection, whose
    transport may not be up yet (see CivKey's docstring)."""

    def __init__(self, conn, line: str):
        self._conn = conn
        self._line = line

    def set(self, on: bool):
        conn = self._conn() if callable(self._conn) else self._conn
        if conn is None:
            log.warning("PTT/CW keyed via %s line but the serial connection isn't up — dropped", self._line)
            return
        setattr(conn, self._line, on)


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
        self._keepalive_task = None
        self._extra_serials: dict = {}  # port name -> pyserial.Serial, for PTT/CW on a distinct port
        self._test_future = None
        self._cw_release_handle = None
        self._ptt_release_handle = None  # pending delayed hardware un-key — see PTT_TAIL_MS in _handle_ptt
        self._data_observers: list = []  # extra callbacks fed every CAT byte — e.g. amplifier CAT mirror
        self.amp_fault_check = None      # optional callable() -> bool, set by AmplifierManager when linked

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
            mic_gain=audio_cfg.get("input_gain", 1.0),
            speaker_gain=audio_cfg.get("output_gain", 1.0),
            latency=audio_cfg.get("latency", "low"),
            codec=audio_cfg.get("codec", "pcm16"),
            sample_rate=audio_cfg.get("sample_rate", 48000),
        )
        self.audio.start()

        self.cw_link = CwLink(self.cfg["cw_udp_port"])
        asyncio.get_running_loop().add_reader(self.cw_link.sock.fileno(), self._on_cw_readable)

        host = self.cfg["cat"].get("tcp_host", "0.0.0.0")
        self._cat_server = await make_cat_server(self.serial_proto, host, self.cfg["cat"]["tcp_port"])
        self._control_server = await asyncio.start_server(self._handle_control_client, host, self.cfg["control_port"])
        self._keepalive_task = asyncio.create_task(self._control_keepalive_loop())
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
        if self._keepalive_task:
            self._keepalive_task.cancel()
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
        if self._ptt_release_handle:
            self._ptt_release_handle.cancel()
        for conn in self._extra_serials.values():
            conn.close()
        if self.serial_proto and self.serial_proto.transport:
            self.serial_proto.transport.close()
        for w in list(self.control_clients):
            w.close()

    def _on_serial_data(self, data: bytes):
        if self._test_future and not self._test_future.done():
            self._test_future.set_result(True)
        for cb in self._data_observers:
            cb(data)

    def add_data_observer(self, callback):
        """Called with every raw CAT byte chunk from the radio — e.g. an
        AmplifierBridge mirroring this radio's CAT stream for band tracking."""
        self._data_observers.append(callback)

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
            return CivKey(self.serial_proto, key_cfg["civ_address"])
        if method in ("rts", "dtr"):
            port = key_cfg.get("serial_port")
            if port and port != self.cfg["cat"]["serial_port"]:
                if port not in self._extra_serials:
                    conn = serial.Serial(port, key_cfg.get("baud", self.cfg["cat"]["baud"]))
                    conn.rts = False
                    conn.dtr = False
                    self._extra_serials[port] = conn
                conn = self._extra_serials[port]
            else:
                # reuse the CAT connection's line — lazy: its transport
                # may not be up yet at this exact instant, see CivKey.
                conn = lambda: self.serial_proto.transport.serial if self.serial_proto.transport else None
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
                try:
                    line = await asyncio.wait_for(reader.readline(), timeout=CONTROL_READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    # No hello/ptt/ping from this client in CONTROL_READ_TIMEOUT_S
                    # — a silent network drop (no FIN/RST) never trips the
                    # normal "if not line: break" path, so a held PTT would
                    # otherwise stay keyed forever. The keepalive ping gives
                    # a healthy client something to send well before this
                    # fires; a real client always replies via the ping
                    # itself keeping traffic flowing, so this only catches
                    # connections that are actually gone.
                    if username:
                        log.warning("control client %s went silent for %.0fs — treating as disconnected", username, CONTROL_READ_TIMEOUT_S)
                    break
                if not line:
                    break
                msg = json.loads(line)
                if msg["type"] == "hello":
                    candidate = msg["username"]
                    if not await self._authorize(candidate, msg.get("password", "")):
                        await self._send(writer, {"type": "hello_denied", "reason": "грешни данни или няма право за това радио"})
                        break
                    username = candidate
                    self.control_clients[writer] = username
                    self.session_ids[writer] = await self.db.start_session(username, self.name)
                    await self._send(writer, {"type": "hello_ack", "radio": self.name})
                    await self._broadcast_status()
                elif msg["type"] == "ptt" and username:
                    await self._handle_ptt(writer, username, msg["on"])
                elif msg["type"] == "ping":
                    pass  # just proof of life — see CONTROL_READ_TIMEOUT_S above
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

    async def _authorize(self, username: str, password: str) -> bool:
        if not isinstance(self.db, PostgresDb):
            return True  # no accounts configured — same degraded-open behavior as the admin panel without db.dsn
        user = await self.db.authenticate(username, password)
        if not user:
            return False
        return bool(user["is_admin"] or await self.db.user_can_access_radio(username, self.name))

    async def _handle_ptt(self, writer, username, on):
        if on:
            if self.amp_fault_check and self.amp_fault_check():
                log.warning("PTT ON denied for %s on %s: amplifier fault", username, self.name)
                await self._send(writer, {"type": "ptt_denied", "reason": "усилвателят докладва грешка/overtemp"})
                return
            try:
                self.arbiter.acquire(username)
            except PttDenied as e:
                log.info("PTT ON denied for %s on %s: %s", username, self.name, e)
                await self._send(writer, {"type": "ptt_denied", "reason": str(e)})
                return
            if self._ptt_release_handle:
                # Operator keyed up again before a previous release's
                # audio tail finished draining — the hardware was never
                # actually un-keyed. Cancel the pending release and keep
                # the same transmission/tx_id going, instead of a
                # spurious off-then-on blip and an orphaned db row.
                self._ptt_release_handle.cancel()
                self._ptt_release_handle = None
            else:
                self.ptt_method.set(True)
                session_id = self.session_ids.get(writer)
                self.tx_ids[writer] = await self.db.start_transmission(session_id) if session_id is not None else None
            await self._send(writer, {"type": "ptt_ack", "on": True})
            log.info("PTT ON by %s on %s", username, self.name)
        else:
            if self.arbiter.holder != username:
                log.info("PTT OFF from %s on %s ignored — holder is %s", username, self.name, self.arbiter.holder)
                await self._send(writer, {"type": "ptt_ack", "on": False})
                return
            await self._send(writer, {"type": "ptt_ack", "on": False})  # UI feels responsive even if the hardware lingers
            log.info("PTT OFF (requested) by %s on %s", username, self.name)
            tail_ms = (self.cfg.get("audio") or {}).get("ptt_tail_ms", 0)
            if tail_ms > 0:
                # The audio the operator just spoke is still in flight
                # (mic buffering -> network -> server playback buffering)
                # when PTT is released — un-keying immediately clips the
                # tail of the last word. Hold the radio keyed (and the
                # channel marked busy, so nobody else jumps in) until
                # that audio has had time to actually play out.
                loop = asyncio.get_running_loop()
                self._ptt_release_handle = loop.call_later(tail_ms / 1000, self._release_ptt, writer, username)
                return
            self._release_ptt(writer, username)
        await self._broadcast_status()

    def _release_ptt(self, writer, username):
        self._ptt_release_handle = None
        if self.arbiter.holder == username:
            self.arbiter.release(username)
            self.ptt_method.set(False)
            log.info("PTT OFF (hardware) by %s on %s", username, self.name)
        tx_id = self.tx_ids.pop(writer, None)
        if tx_id is not None:
            asyncio.create_task(self.db.end_transmission(tx_id))
        asyncio.create_task(self._broadcast_status())

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
                # A send failing IS proof this connection is dead — clean
                # it up right here instead of swallowing it. Previously
                # this just `pass`ed: a client that vanished without a
                # clean TCP close (silent network drop, no FIN/RST) kept
                # its writer in control_clients forever, and if it was
                # PTT holder at the time, the radio stayed keyed forever
                # too — nothing was ever going to call
                # _release_if_holder for a connection nobody noticed died.
                username = self.control_clients.pop(w, None)
                if username:
                    log.warning("control client %s send failed — treating connection as dead", username)
                    await self._release_if_holder(w, username)
                with contextlib.suppress(Exception):
                    w.close()

    async def _broadcast_status(self):
        await self._broadcast({"type": "status", "busy_by": self.arbiter.holder})

    async def _control_keepalive_loop(self):
        """Belt-and-braces alongside the read timeout in
        _handle_control_client: proves to every connected client that
        we're still here (and, via _broadcast's cleanup above, notices
        when one of them silently isn't anymore) even during a stretch
        with no PTT/status activity to naturally carry traffic."""
        while True:
            await asyncio.sleep(CONTROL_KEEPALIVE_INTERVAL_S)
            await self._broadcast({"type": "ping"})


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

    def _demo_lazy_keys():
        # Reproduces the reported crash: PTT/CW keyed while
        # serial_proto.transport is still None (serial_asyncio's
        # connection_made hasn't run via call_soon yet, or the port
        # physically disconnected) must drop the key event, not raise.
        class _FakeSerialProto:
            def __init__(self):
                self.transport = None

        class _FakeTransport:
            def __init__(self):
                self.written = []

            def write(self, data):
                self.written.append(data)

        proto = _FakeSerialProto()
        key = CivKey(proto, 148)
        key.set(True)  # transport still None — must not raise

        proto.transport = _FakeTransport()
        key.set(True)
        assert proto.transport.written == [ptt_command(148, True)]

        class _FakeSerialConn:
            rts = None

        holder = {"conn": None}
        line_key = LineKey(lambda: holder["conn"], "rts")
        line_key.set(True)  # still None — must not raise

        holder["conn"] = _FakeSerialConn()
        line_key.set(True)
        assert holder["conn"].rts is True

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

    class _FakeAuthDb(PostgresDb):
        def __init__(self):
            pass  # skip the real pool/connect

        async def authenticate(self, username, password):
            if username == "ivan" and password == "secret":
                return {"username": "ivan", "is_admin": False}
            if username == "admin" and password == "secret":
                return {"username": "admin", "is_admin": True}
            return None

        async def user_can_access_radio(self, username, radio_name):
            return radio_name == "IC-7300"

    async def _demo_authorize():
        # No db configured (dev/NullDb) — degraded-open, matches the admin
        # panel's own behavior without db.dsn.
        open_bridge = RadioBridge({"name": "IC-7300"}, NullDb())
        assert await open_bridge._authorize("anyone", "wrong") is True

        bridge = RadioBridge({"name": "IC-7300"}, _FakeAuthDb())
        assert await bridge._authorize("ivan", "secret") is True       # right password, permitted radio
        assert await bridge._authorize("ivan", "wrong") is False       # wrong password
        assert await bridge._authorize("admin", "secret") is True      # admin bypasses per-radio grants

        other_bridge = RadioBridge({"name": "IC-746PRO"}, _FakeAuthDb())
        assert await other_bridge._authorize("ivan", "secret") is False  # right password, NOT permitted for this radio

    class _FakeWriter:
        def __init__(self):
            self.sent = []

        def write(self, data):
            self.sent.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

    async def _demo_ptt_release():
        # Reproduces the report: hold PTT (on=True), release (on=False) —
        # the radio must actually un-key, not just ack the release.
        bridge = RadioBridge({"name": "TEST"}, NullDb())
        bridge.ptt_method = _FakeKey()
        writer = _FakeWriter()

        await bridge._handle_ptt(writer, "ivan", True)
        assert bridge.arbiter.holder == "ivan"
        assert bridge.ptt_method.calls == [True], "radio never keyed on PTT-down"

        await bridge._handle_ptt(writer, "ivan", False)
        assert bridge.arbiter.holder is None, "arbiter never released on PTT-up"
        assert bridge.ptt_method.calls == [True, False], "radio never un-keyed on PTT-up"

    async def _demo_ptt_tail_delay():
        # PTT off with a configured tail must NOT un-key immediately —
        # buffered audio (mic -> network -> server playback) needs time
        # to actually reach the radio before it stops transmitting.
        bridge = RadioBridge({"name": "TEST", "audio": {"ptt_tail_ms": 50}}, NullDb())
        bridge.ptt_method = _FakeKey()
        writer = _FakeWriter()

        await bridge._handle_ptt(writer, "ivan", True)
        await bridge._handle_ptt(writer, "ivan", False)
        assert bridge.arbiter.holder == "ivan", "tail delay must keep the channel held, not free it early"
        assert bridge.ptt_method.calls == [True], "tail delay must not un-key immediately"

        await asyncio.sleep(0.1)  # longer than the 50ms tail
        assert bridge.arbiter.holder is None, "tail delay never released the channel"
        assert bridge.ptt_method.calls == [True, False], "tail delay never un-keyed the hardware"

    async def _demo_ptt_tail_cancelled_by_rekey():
        # Operator releases then keys up again within the tail window —
        # must not blip the hardware off-then-on, and the later
        # (cancelled) release must never fire.
        bridge = RadioBridge({"name": "TEST", "audio": {"ptt_tail_ms": 200}}, NullDb())
        bridge.ptt_method = _FakeKey()
        writer = _FakeWriter()

        await bridge._handle_ptt(writer, "ivan", True)
        await bridge._handle_ptt(writer, "ivan", False)
        await bridge._handle_ptt(writer, "ivan", True)  # re-key before the 200ms tail elapses
        assert bridge.ptt_method.calls == [True], "re-keying during the tail must not blip the hardware off"
        assert bridge.arbiter.holder == "ivan"

        await asyncio.sleep(0.3)  # well past the (cancelled) tail
        assert bridge.ptt_method.calls == [True], "cancelled tail must not fire a stale release later"
        assert bridge.arbiter.holder == "ivan", "still transmitting — must still hold the channel"

    async def _demo_broadcast_cleans_up_dead_writer():
        # The other half of the same class of bug: _broadcast() (used by
        # every status update, including the keepalive ping) used to just
        # swallow a failed send and move on — a writer that's actually
        # dead stayed registered (and, if it was PTT holder, stayed
        # "holding" it) forever, since nothing else would ever notice.
        class _RaisingWriter:
            def write(self, data):
                raise ConnectionResetError("gone")

            def close(self):
                pass

        bridge = RadioBridge({"name": "TEST"}, NullDb())
        bridge.ptt_method = _FakeKey()
        dead_writer = _RaisingWriter()
        bridge.control_clients[dead_writer] = "ivan"
        bridge.arbiter.acquire("ivan")
        bridge.ptt_method.set(True)

        await bridge._broadcast({"type": "status", "busy_by": "ivan"})

        assert dead_writer not in bridge.control_clients, "dead writer never removed from control_clients"
        assert bridge.arbiter.holder is None, "PTT never released for a writer that failed to send"
        assert bridge.ptt_method.calls == [True, False], "radio never un-keyed when the dead writer was found"

    async def _demo_silent_disconnect():
        # Reproduces the report: hold PTT, connection dies WITHOUT a clean
        # TCP close (real-world: WiFi/NAT silently drops it) — nothing
        # ever sends "off" and nobody ever sees EOF. Must still un-key
        # once the read timeout notices, not stay keyed forever.
        global CONTROL_READ_TIMEOUT_S
        original_timeout = CONTROL_READ_TIMEOUT_S
        CONTROL_READ_TIMEOUT_S = 1.0  # short but not so tight the hello/ptt round-trips themselves risk tripping it
        try:
            bridge = RadioBridge({"name": "TEST"}, NullDb())
            bridge.ptt_method = _FakeKey()
            server = await asyncio.start_server(bridge._handle_control_client, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            async def _read_until(reader, msg_type):
                # hello/ptt each trigger a _broadcast_status() alongside
                # their own ack — read past that interleaved "status"
                # line rather than assuming a fixed 1-reply-per-request
                # ordering.
                while True:
                    line = json.loads(await reader.readline())
                    if line["type"] == msg_type:
                        return line

            async with server:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write((json.dumps({"type": "hello", "username": "ivan", "password": ""}) + "\n").encode())
                await writer.drain()
                await _read_until(reader, "hello_ack")
                writer.write((json.dumps({"type": "ptt", "on": True}) + "\n").encode())
                await writer.drain()
                await _read_until(reader, "ptt_ack")
                assert bridge.arbiter.holder == "ivan"
                assert bridge.ptt_method.calls == [True]

                # Go silent WITHOUT closing — the socket stays technically
                # open, just nobody sends or reads anything, same as a
                # network path that drops packets without a proper FIN/RST.
                await asyncio.sleep(CONTROL_READ_TIMEOUT_S + 0.3)
                assert bridge.arbiter.holder is None, "PTT never released after the connection went silent"
                assert bridge.ptt_method.calls == [True, False], "radio never un-keyed after a silent disconnect"
                writer.close()
        finally:
            CONTROL_READ_TIMEOUT_S = original_timeout

    _demo_lazy_keys()
    asyncio.run(_demo())
    asyncio.run(_demo_ptt_release())
    asyncio.run(_demo_ptt_tail_delay())
    asyncio.run(_demo_ptt_tail_cancelled_by_rekey())
    asyncio.run(_demo_broadcast_cleans_up_dead_writer())
    asyncio.run(_demo_silent_disconnect())
    asyncio.run(_demo_authorize())
    print("radio_bridge.py: ok")
