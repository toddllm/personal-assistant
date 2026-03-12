#!/usr/bin/env python3
"""Record a few seconds from CaptureMic 2ch and play it back.

Usage: python3 scripts/mic-test.py [seconds]

Used by the volume-control API for mic testing.
"""

import os
import subprocess
import sys
import wave

import numpy as np
import sounddevice as sd

DEVICE_NAME = "CaptureMic 2ch"
RATE = 48000
CHANNELS = 2
DEFAULT_SECONDS = 3
WAV_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "data", "mic-test.wav",
)


def find_device(name: str, kind: str) -> int | None:
    for i, dev in enumerate(sd.query_devices()):
        if name.lower() in dev["name"].lower():
            if kind == "input" and dev["max_input_channels"] < 1:
                continue
            if kind == "output" and dev["max_output_channels"] < 1:
                continue
            return i
    return None


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SECONDS

    idx = find_device(DEVICE_NAME, "input")
    if idx is None:
        print(f"ERROR: {DEVICE_NAME} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Recording {seconds}s from {DEVICE_NAME}...")
    frames = sd.rec(
        int(seconds * RATE),
        samplerate=RATE,
        channels=CHANNELS,
        device=idx,
        dtype="float32",
    )
    sd.wait()

    # Save to WAV
    os.makedirs(os.path.dirname(WAV_PATH), exist_ok=True)
    pcm = (frames * 32767).clip(-32768, 32767).astype(np.int16)
    with wave.open(WAV_PATH, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(pcm.tobytes())

    print(f"Saved to {WAV_PATH}")

    # Play back via afplay (macOS)
    print("Playing back...")
    subprocess.run(["afplay", WAV_PATH], check=True)
    print("Done.")


if __name__ == "__main__":
    main()
