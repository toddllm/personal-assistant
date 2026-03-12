"""Audio mixing utility for combining per-user streams.

Additive sum with hard clip — same approach as mic-forward.py.
"""

from __future__ import annotations

import struct


def mix_pcm_streams(streams: list[bytes], sample_width: int = 2) -> bytes:
    """Mix multiple PCM s16le mono streams by additive sum + hard clip.

    All streams must have the same sample rate and format.
    The output length matches the longest input stream.

    Args:
        streams: List of raw s16le mono PCM byte strings.
        sample_width: Bytes per sample (2 for s16le).

    Returns:
        Mixed PCM data as s16le mono bytes.
    """
    if not streams:
        return b""
    if len(streams) == 1:
        return streams[0]

    # Find max length and unpack all
    max_len = max(len(s) for s in streams)
    max_samples = max_len // sample_width

    mixed = [0] * max_samples

    for stream in streams:
        n = len(stream) // sample_width
        if n == 0:
            continue
        samples = struct.unpack(f"<{n}h", stream[:n * sample_width])
        for i, s in enumerate(samples):
            mixed[i] += s

    # Hard clip to s16le range
    clipped = [max(-32768, min(32767, s)) for s in mixed]
    return struct.pack(f"<{len(clipped)}h", *clipped)
