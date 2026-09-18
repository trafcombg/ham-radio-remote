"""Bidirectional mic/speaker <-> UDP link, shared by server and client.

ponytail: codec="opus" uses `av` (PyAV, wraps FFmpeg's libopus) — a
previous attempt with PyOgg's bundled opus.dll was broken on decode
(confirmed with two independent ctypes bindings: encoded fine, decode
always returned silence); `av` was verified with a real encode/decode
round-trip (waveform correlation, not just "no exception") before use.
It pulls in FFmpeg's full codec set as a side effect (~100+MB installer
growth) to get the ~500KB of libopus we actually want — a leaner
ctypes-against-a-bare-libopus.dll build would avoid that, but wasn't
needed to get a working codec shipped. codec="ulaw" (G.711, ~2x smaller,
no extra dependency beyond audioop-lts) remains for a lighter-weight
option. Set per radio from the admin panel; the client reads whatever
the server has configured via /api/client/radios and matches it — the
two ends must agree, there's no in-band negotiation.
"""

import audioop
import logging
import socket
import threading
import time

import av
import numpy as np
import sounddevice as sd

log = logging.getLogger("audio_io")

SAMPLE_RATE = 48000  # default when a radio doesn't specify one
CHANNELS = 1
FRAME_MS = 20
OPUS_BITRATE = 24000  # good voice quality at low bandwidth; not user-configurable (yet)

PEER_WAIT_LOG_INTERVAL_S = 3.0  # throttle — _on_mic/_on_speaker run every 20ms
ANNOUNCE_INTERVAL_S = 3.0  # how often to re-poke a known peer — see start()
JITTER_SMOOTHING = 1 / 16  # RFC 3550-style exponential moving average factor


def _apply_gain(samples: np.ndarray, gain: float) -> np.ndarray:
    if gain == 1.0:
        return samples
    return np.clip(samples.astype(np.int32) * gain, -32768, 32767).astype(np.int16)


def _encode(samples: np.ndarray, codec: str) -> bytes:
    pcm_bytes = samples.tobytes()
    if codec == "ulaw":
        return audioop.lin2ulaw(pcm_bytes, 2)
    return pcm_bytes


def _decode(data: bytes, codec: str) -> np.ndarray:
    pcm_bytes = audioop.ulaw2lin(data, 2) if codec == "ulaw" else data
    return np.frombuffer(pcm_bytes, dtype=np.int16).reshape(-1, CHANNELS)


def _update_jitter(prev_jitter_ms: float, gap_s: float) -> float:
    """RFC 3550-style smoothed jitter: how far the spacing between
    received packets deviates from the expected FRAME_MS, averaged so one
    stray late packet doesn't spike the reading."""
    deviation_ms = abs(gap_s * 1000 - FRAME_MS)
    return prev_jitter_ms + (deviation_ms - prev_jitter_ms) * JITTER_SMOOTHING


class AudioLink:
    def __init__(
        self, input_device, output_device, listen_port, peer=None, mic_gain=1.0, speaker_gain=1.0, latency="low",
        codec="pcm16", sample_rate=SAMPLE_RATE,
    ):
        self.peer = peer
        self.input_level = 0.0   # last mic frame's mean abs amplitude, for a UI meter
        self.output_level = 0.0  # last speaker frame's mean abs amplitude, for a UI meter
        self.mic_gain = mic_gain          # 1.0 = unity; adjustable live from the UI
        self.speaker_gain = speaker_gain

        self.codec = codec  # "pcm16" (uncompressed), "ulaw" (G.711, ~2x smaller), or "opus"
        self.sample_rate = sample_rate
        self.frame_samples = sample_rate * FRAME_MS // 1000
        self.frame_bytes = self.frame_samples * CHANNELS * 2  # int16 width, pre-codec

        # Opus is stateful across frames (unlike pcm16/ulaw) — one
        # persistent encoder/decoder pair per link, not per call.
        self._opus_encoder = None
        self._opus_decoder = None
        if self.codec == "opus":
            self._opus_encoder = av.CodecContext.create("libopus", "w")
            self._opus_encoder.sample_rate = self.sample_rate
            self._opus_encoder.format = "s16"
            self._opus_encoder.layout = "mono"
            self._opus_encoder.bit_rate = OPUS_BITRATE
            self._opus_decoder = av.CodecContext.create("libopus", "r")
            self._opus_decoder.sample_rate = self.sample_rate
            self._opus_decoder.format = "s16"
            self._opus_decoder.layout = "mono"

        self.latency_ms = 0.0  # PortAudio-reported device buffering latency, set in start()
        self.jitter_ms = 0.0   # smoothed deviation of received-packet spacing from FRAME_MS
        self._last_recv_time = None
        # Cumulative — status_panel.py derives a speed from the deltas.
        # Written from the PortAudio callback thread (see _on_mic/_on_speaker
        # below); a plain int has no lock around it, so a display-only
        # counter can in theory drop an update under a race, never corrupt.
        self.bytes_sent = 0
        self.bytes_recv = 0

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # DSCP Expedited Forwarding (0xB8) — standard "this is
            # real-time voice traffic" marker. Best-effort: recent Windows
            # versions ignore a plain setsockopt() for unprivileged apps
            # (real QoS marking there needs the qWave API instead), but
            # this costs nothing and helps on LANs/OSes that honor it.
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 0xB8)
        except OSError:
            pass
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
                    device=input_device, samplerate=self.sample_rate, channels=CHANNELS,
                    dtype="int16", blocksize=self.frame_samples, callback=self._on_mic, latency=latency,
                )
            except Exception:
                log.warning("no microphone/input device available (device=%r) — mic capture disabled", input_device, exc_info=True)
            try:
                self._out_stream = sd.OutputStream(
                    device=output_device, samplerate=self.sample_rate, channels=CHANNELS,
                    dtype="int16", blocksize=self.frame_samples, callback=self._on_speaker, latency=latency,
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

    def _encode_frame(self, samples: np.ndarray) -> bytes:
        if self.codec != "opus":
            return _encode(samples, self.codec)
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(samples))
        frame.sample_rate = self.sample_rate
        frame.planes[0].update(samples.tobytes())
        return b"".join(bytes(p) for p in self._opus_encoder.encode(frame))

    def _decode_frame(self, data: bytes) -> np.ndarray:
        if self.codec != "opus":
            return _decode(data, self.codec)
        if not data:
            return np.zeros((0, CHANNELS), dtype=np.int16)
        frames = self._opus_decoder.decode(av.Packet(data))
        if not frames:
            return np.zeros((0, CHANNELS), dtype=np.int16)
        return np.concatenate([f.to_ndarray().flatten() for f in frames]).astype(np.int16).reshape(-1, CHANNELS)

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
            payload = self._encode_frame(out)
            if payload:
                self.sock.sendto(payload, self.peer)
                self.bytes_sent += len(payload)
        except Exception:
            # Broad on purpose: this runs inside the PortAudio C callback,
            # which can't propagate exceptions — an Opus encoder error
            # here would otherwise silently break the callback the same
            # way an uncaught ConnectionResetError once did in _on_speaker.
            log.exception("mic encode/send failed")

    # ponytail: no jitter buffer/reordering — one packet in, one block out.
    # Add a small jitter buffer if real LAN jitter causes audible glitches.
    def _on_speaker(self, outdata, frames, time_info, status):
        if status:
            log.warning("output status: %s", status)
        try:
            data, addr = self.sock.recvfrom(self.frame_bytes)
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
        self.bytes_recv += len(data)
        if self.peer is None:
            self.peer = addr
            log.info("audio peer learned: %s", addr)
        now = time.monotonic()
        if self._last_recv_time is not None:
            self.jitter_ms = _update_jitter(self.jitter_ms, now - self._last_recv_time)
        self._last_recv_time = now
        try:
            samples = self._decode_frame(data)
        except Exception:
            # A garbled/foreign packet must not crash the callback — same
            # reasoning as the OSError guard above, just for decode errors
            # instead of socket errors (Opus decode can raise on bad input
            # in a way plain PCM/ulaw framing never could).
            log.warning("decode failed on UDP :%s — playing silence", self._listen_port, exc_info=True)
            outdata.fill(0)
            self.output_level = 0.0
            return
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
        self.latency_ms = round(
            ((self._in_stream.latency if self._in_stream else 0) + (self._out_stream.latency if self._out_stream else 0)) * 1000, 1
        )
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
            # close(), not stop(): stop() leaves the device allocated —
            # a new AudioLink opening the same physical device right
            # after (exactly what happens when the server reconfigures a
            # radio's codec/sample_rate and the client rebuilds live)
            # raced against the still-held device and came out as
            # crackling/garbled audio until the whole process restarted
            # and force-released it.
            self._in_stream.close()
        if self._out_stream:
            self._out_stream.close()
        self.sock.close()


if __name__ == "__main__":
    unity = np.array([1000, -1000, 32000], dtype=np.int16)
    assert (_apply_gain(unity, 1.0) == unity).all()

    doubled = _apply_gain(np.array([1000, -1000], dtype=np.int16), 2.0)
    assert list(doubled) == [2000, -2000]

    clipped = _apply_gain(np.array([20000, -20000], dtype=np.int16), 2.0)
    assert list(clipped) == [32767, -32768]  # clamped, not wrapped around

    j = _update_jitter(0.0, FRAME_MS / 1000)  # packet arrives exactly on time
    assert j == 0.0
    j = _update_jitter(j, (FRAME_MS + 20) / 1000)  # next one is 20ms late
    assert 1.0 < j < 1.5  # 20ms deviation * 1/16 smoothing

    # Opus round trip: the previous (PyOgg) attempt "worked" in the sense
    # that it ran without raising, but decode silently returned all zeros
    # — so this checks the actual waveform survives, not just "no crash".
    opus_link = AudioLink(input_device=99999, output_device=None, listen_port=0, codec="opus")
    try:
        frame_n = opus_link.frame_samples
        n_frames = 50  # 1s @ 20ms frames
        t = np.arange(n_frames * frame_n) / opus_link.sample_rate
        tone = (0.3 * np.sin(2 * np.pi * 440.0 * t) * 32767).astype(np.int16)
        decoded_chunks = []
        for i in range(n_frames):
            chunk = tone[i * frame_n:(i + 1) * frame_n]
            packet = opus_link._encode_frame(chunk)
            if packet:
                decoded_chunks.append(opus_link._decode_frame(packet).flatten())
        decoded = np.concatenate(decoded_chunks) if decoded_chunks else np.array([], dtype=np.int16)
        assert len(decoded) > SAMPLE_RATE // 2, "opus decode produced far too little audio back"
        rms = float(np.sqrt(np.mean(decoded.astype(np.float64) ** 2)))
        assert rms > 1000, f"opus decode returned near-silence (RMS={rms:.1f}) — same failure mode as the broken PyOgg attempt"
    finally:
        opus_link.stop()

    pcm16 = np.array([1000, -1000, 32000, -32768, 0], dtype=np.int16)
    assert _encode(pcm16, "pcm16") == pcm16.tobytes()  # passthrough, no codec
    ulaw = _encode(pcm16, "ulaw")
    assert len(ulaw) == len(pcm16)  # G.711: 1 byte/sample vs 2 for int16
    decoded = _decode(ulaw, "ulaw")
    assert np.abs(decoded.flatten().astype(np.int32) - pcm16.astype(np.int32)).max() < 1000  # lossy but close
    assert _decode(b"", "ulaw").size == 0  # empty announce packet must not raise

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

    # A radio configured for a lower sample rate must size its frames
    # (and thus its UDP packets) accordingly — this is the actual
    # bandwidth lever, independent of the codec.
    narrow = AudioLink(input_device=99999, output_device=None, listen_port=0, sample_rate=16000, codec="ulaw")
    assert narrow.frame_samples == 320  # 16000 * 20ms
    assert narrow.frame_bytes == 640    # pre-codec (int16) size — recv buffer must fit the uncompressed case
    narrow.stop()

    print("audio_io.py: ok")
