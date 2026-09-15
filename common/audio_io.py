"""Bidirectional mic/speaker <-> UDP link, shared by server and client.

ponytail: raw PCM, not Opus — PyOgg's bundled opus.dll turned out to be
broken on decode (confirmed with two independent ctypes bindings: both
encode fine, decode always returns silence). LAN bandwidth easily covers
uncompressed 48kHz mono int16 (~768kbps) and it's lower-latency besides.
Swap in a verified Opus binding if bandwidth ever becomes a real
constraint (e.g. remote/WAN use beyond the LAN this system targets).
"""

import logging
import socket
import threading
import time

import numpy as np
import sounddevice as sd

log = logging.getLogger("audio_io")

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2  # int16

PEER_WAIT_LOG_INTERVAL_S = 3.0  # throttle — _on_mic/_on_speaker run every 20ms
ANNOUNCE_INTERVAL_S = 3.0  # how often to re-poke a known peer — see start()


def _apply_gain(samples: np.ndarray, gain: float) -> np.ndarray:
    if gain == 1.0:
        return samples
    return np.clip(samples.astype(np.int32) * gain, -32768, 32767).astype(np.int16)


class AudioLink:
    def __init__(self, input_device, output_device, listen_port, peer=None, mic_gain=1.0, speaker_gain=1.0):
        self.peer = peer
        self.input_level = 0.0   # last mic frame's mean abs amplitude, for a UI meter
        self.output_level = 0.0  # last speaker frame's mean abs amplitude, for a UI meter
        self.mic_gain = mic_gain          # 1.0 = unity; adjustable live from the UI
        self.speaker_gain = speaker_gain

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("0.0.0.0", listen_port))
        self._listen_port = listen_port
        self._last_send_wait_log = 0.0
        self._last_recv_wait_log = 0.0
        self._announce_timer = None

        # Input and output are opened independently and degrade gracefully:
        # a machine with no microphone (or no speakers) can still do the
        # half that works — e.g. a listen-only test box with zero input
        # devices, which previously made the WHOLE AudioLink raise and,
        # since nothing awaited switch_to()'s future, failed completely
        # silently. Only raise if neither side is usable.
        self._in_stream = None
        self._out_stream = None
        try:
            try:
                self._in_stream = sd.InputStream(
                    device=input_device, samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", blocksize=FRAME_SAMPLES, callback=self._on_mic,
                )
            except Exception:
                log.warning("no microphone/input device available (device=%r) — mic capture disabled", input_device, exc_info=True)
            try:
                self._out_stream = sd.OutputStream(
                    device=output_device, samplerate=SAMPLE_RATE, channels=CHANNELS,
                    dtype="int16", blocksize=FRAME_SAMPLES, callback=self._on_speaker,
                )
            except Exception:
                log.warning("no speaker/output device available (device=%r) — playback disabled", output_device, exc_info=True)
            if self._in_stream is None and self._out_stream is None:
                raise RuntimeError("нито едно аудио устройство (вход/изход) не е налично")
        except Exception:
            # ponytail: недовършеният AudioLink никога не се присвоява на
            # RadioBridge.audio/session.audio, така че shutdown() не може
            # да го затвори — без това, сокетът/потокът остават заети и
            # следващият опит за старт гърми с "address already in use".
            if self._in_stream is not None:
                self._in_stream.close()
            if self._out_stream is not None:
                self._out_stream.close()
            self.sock.close()
            raise

    def _on_mic(self, indata, frames, time_info, status):
        if status:
            log.warning("input status: %s", status)
        self.input_level = float(np.abs(indata).mean())
        if not self.peer:
            now = time.monotonic()
            if now - self._last_send_wait_log > PEER_WAIT_LOG_INTERVAL_S:
                log.debug("no audio peer yet on UDP :%s — captured frame dropped (nothing received from the other side yet)", self._listen_port)
                self._last_send_wait_log = now
            return
        try:
            out = _apply_gain(indata, self.mic_gain)
            self.sock.sendto(out.tobytes(), self.peer)
        except OSError:
            log.exception("mic send failed")

    # ponytail: no jitter buffer/reordering — one packet in, one block out.
    # Add a small jitter buffer if real LAN jitter causes audible glitches.
    def _on_speaker(self, outdata, frames, time_info, status):
        if status:
            log.warning("output status: %s", status)
        try:
            data, addr = self.sock.recvfrom(FRAME_BYTES)
        except BlockingIOError:
            outdata.fill(0)
            self.output_level = 0.0
            now = time.monotonic()
            if now - self._last_recv_wait_log > PEER_WAIT_LOG_INTERVAL_S:
                log.debug("nothing received yet on UDP :%s — playing silence", self._listen_port)
                self._last_recv_wait_log = now
            return
        except OSError:
            # Windows-specific: a prior send to this peer that came back
            # as ICMP "port unreachable" (the other side restarted, its
            # radio was reloaded, etc.) shows up as ConnectionResetError
            # on the *next* recvfrom — not corruption, just means nobody
            # was listening at that instant. An uncaught exception here
            # escapes into the sounddevice/PortAudio C callback boundary,
            # which can't propagate it — silently breaking playback
            # instead of just skipping one frame. Treat it exactly like
            # "nothing received yet" (the periodic announce in start()
            # keeps re-teaching the peer once it comes back).
            outdata.fill(0)
            self.output_level = 0.0
            now = time.monotonic()
            if now - self._last_recv_wait_log > PEER_WAIT_LOG_INTERVAL_S:
                log.debug("recvfrom error on UDP :%s (peer restarting?) — playing silence", self._listen_port, exc_info=True)
                self._last_recv_wait_log = now
            return
        if self.peer is None:
            self.peer = addr
            log.info("audio peer learned: %s", addr)
        samples = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS)
        samples = _apply_gain(samples, self.speaker_gain)
        self.output_level = float(np.abs(samples).mean()) if len(samples) else 0.0
        n = min(len(samples), frames)
        outdata[:n] = samples[:n]
        if n < frames:
            outdata[n:] = 0

    def _announce(self):
        """Repeated, not one-shot: if the peer already knows us (normal
        case), this is just a few harmless extra empty packets alongside
        real mic audio. If the *other side* ever forgets us — it
        restarted, reloaded the radio, whatever — a single one-time poke
        at start() would never be re-sent and the link would stay dead
        until something else (a manual reconnect) rebuilt it. Repeating
        this every ANNOUNCE_INTERVAL_S self-heals that within a few
        seconds instead, with no reconnect needed on our side at all."""
        if self.peer:
            try:
                self.sock.sendto(b"", self.peer)
            except OSError:
                log.exception("audio announce to %s failed", self.peer)
        self._announce_timer = threading.Timer(ANNOUNCE_INTERVAL_S, self._announce)
        self._announce_timer.daemon = True
        self._announce_timer.start()

    def start(self):
        if self._in_stream:
            self._in_stream.start()
        if self._out_stream:
            self._out_stream.start()
        if self.peer:
            self._announce()
        log.info(
            "audio link up on UDP :%s peer=%s (mic=%s, speaker=%s)",
            self.sock.getsockname()[1], self.peer, self._in_stream is not None, self._out_stream is not None,
        )

    def stop(self):
        if self._announce_timer:
            self._announce_timer.cancel()
            self._announce_timer = None
        if self._in_stream:
            self._in_stream.stop()
        if self._out_stream:
            self._out_stream.stop()
        self.sock.close()


if __name__ == "__main__":
    unity = np.array([1000, -1000, 32000], dtype=np.int16)
    assert (_apply_gain(unity, 1.0) == unity).all()

    doubled = _apply_gain(np.array([1000, -1000], dtype=np.int16), 2.0)
    assert list(doubled) == [2000, -2000]

    clipped = _apply_gain(np.array([20000, -20000], dtype=np.int16), 2.0)
    assert list(clipped) == [32767, -32768]  # clamped, not wrapped around

    # Reproduces the reported bug: a client with no working mic never
    # sent anything, so the server-side peer was never learned and the
    # server could never send audio back either — even though the
    # client's speaker was ready and waiting the whole time.
    import time

    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.setblocking(False)
    listen_addr = listener.getsockname()

    link = AudioLink(input_device=99999, output_device=None, listen_port=0, peer=listen_addr)
    assert link._in_stream is None, "an out-of-range device index must fail to open, not raise"
    link.start()
    try:
        time.sleep(0.1)
        data, addr = listener.recvfrom(16)
        assert data == b"", "expected an empty poke packet to teach the other side our address"
    except BlockingIOError:
        raise AssertionError("no mic AND no poke packet — the other side would never learn our peer")
    finally:
        link.stop()
        listener.close()

    print("audio_io.py: ok")
