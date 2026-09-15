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

import numpy as np
import sounddevice as sd

log = logging.getLogger("audio_io")

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960
FRAME_BYTES = FRAME_SAMPLES * CHANNELS * 2  # int16


def _apply_gain(samples: np.ndarray, gain: float) -> np.ndarray:
    if gain == 1.0:
        return samples
    return np.clip(samples.astype(np.int32) * gain, -32768, 32767).astype(np.int16)


class AudioLink:
    def __init__(self, input_device, output_device, listen_port, peer=None, mic_gain=1.0, speaker_gain=1.0):
        self.peer = peer
        self.level = 0.0  # last mic frame's mean abs amplitude, for a UI meter
        self.mic_gain = mic_gain          # 1.0 = unity; adjustable live from the UI
        self.speaker_gain = speaker_gain

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.sock.bind(("0.0.0.0", listen_port))

        self._in_stream = sd.InputStream(
            device=input_device, samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", blocksize=FRAME_SAMPLES, callback=self._on_mic,
        )
        self._out_stream = sd.OutputStream(
            device=output_device, samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="int16", blocksize=FRAME_SAMPLES, callback=self._on_speaker,
        )

    def _on_mic(self, indata, frames, time_info, status):
        if status:
            log.warning("input status: %s", status)
        self.level = float(np.abs(indata).mean())
        if not self.peer:
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
            return
        if self.peer is None:
            self.peer = addr
            log.info("audio peer learned: %s", addr)
        samples = np.frombuffer(data, dtype=np.int16).reshape(-1, CHANNELS)
        samples = _apply_gain(samples, self.speaker_gain)
        n = min(len(samples), frames)
        outdata[:n] = samples[:n]
        if n < frames:
            outdata[n:] = 0

    def start(self):
        self._in_stream.start()
        self._out_stream.start()
        log.info("audio link up on UDP :%s peer=%s", self.sock.getsockname()[1], self.peer)

    def stop(self):
        self._in_stream.stop()
        self._out_stream.stop()
        self.sock.close()


if __name__ == "__main__":
    unity = np.array([1000, -1000, 32000], dtype=np.int16)
    assert (_apply_gain(unity, 1.0) == unity).all()

    doubled = _apply_gain(np.array([1000, -1000], dtype=np.int16), 2.0)
    assert list(doubled) == [2000, -2000]

    clipped = _apply_gain(np.array([20000, -20000], dtype=np.int16), 2.0)
    assert list(clipped) == [32767, -32768]  # clamped, not wrapped around

    print("audio_io.py: ok")
