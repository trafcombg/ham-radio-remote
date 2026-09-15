"""Bidirectional mic/speaker <-> UDP/Opus link, shared by server and client.

Both sides are the same relay: capture mic, Opus-encode, send UDP; receive
UDP, Opus-decode, play to speaker. The only difference is how the peer
address is known — fixed upfront (client -> server) or learned from the
first incoming packet (server, since it doesn't know the client's port).
"""

import logging
import socket

import numpy as np
import sounddevice as sd
from pyogg import OpusEncoder, OpusDecoder

log = logging.getLogger("audio_io")

SAMPLE_RATE = 48000
CHANNELS = 1
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 960


class OpusAudioLink:
    def __init__(self, input_device, output_device, listen_port, peer=None):
        self.peer = peer
        self.level = 0.0  # last mic frame's mean abs amplitude, for a UI meter

        self.encoder = OpusEncoder()
        self.encoder.set_application("audio")
        self.encoder.set_sampling_frequency(SAMPLE_RATE)
        self.encoder.set_channels(CHANNELS)

        self.decoder = OpusDecoder()
        self.decoder.set_sampling_frequency(SAMPLE_RATE)
        self.decoder.set_channels(CHANNELS)

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
            packet = self.encoder.encode(indata.tobytes())
            self.sock.sendto(packet, self.peer)
        except OSError:
            log.exception("mic send failed")

    # ponytail: no jitter buffer / reordering — one packet in, one block out.
    # Add a small jitter buffer if real LAN jitter causes audible glitches.
    def _on_speaker(self, outdata, frames, time_info, status):
        if status:
            log.warning("output status: %s", status)
        try:
            data, addr = self.sock.recvfrom(4000)
        except BlockingIOError:
            outdata.fill(0)
            return
        if self.peer is None:
            self.peer = addr
            log.info("audio peer learned: %s", addr)
        try:
            pcm = self.decoder.decode(bytearray(data))
            samples = np.frombuffer(pcm, dtype=np.int16).reshape(-1, CHANNELS)
        except Exception:
            log.exception("opus decode failed")
            outdata.fill(0)
            return
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
