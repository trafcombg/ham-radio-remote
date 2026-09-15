"""Lists local audio input/output devices for the per-radio device picker
in Settings — thin wrapper around sounddevice.query_devices()."""

import sounddevice as sd


def list_input_devices() -> list:
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]


def list_output_devices() -> list:
    return [(i, d["name"]) for i, d in enumerate(sd.query_devices()) if d["max_output_channels"] > 0]


if __name__ == "__main__":
    inputs = list_input_devices()
    outputs = list_output_devices()
    assert isinstance(inputs, list) and isinstance(outputs, list)
    assert all(isinstance(i, int) and isinstance(n, str) for i, n in inputs)
    assert all(isinstance(i, int) and isinstance(n, str) for i, n in outputs)
    print(f"audio_devices.py: ok ({len(inputs)} input, {len(outputs)} output devices found)")
