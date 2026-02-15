#!/usr/bin/env python3
"""Test the full TTS audio chain offline (no meeting needed).

Verifies:
1. ElevenLabs returns PCM (not MP3) with output_format as query param
2. Raw 22050Hz PCM is valid speech audio
3. Resampled 32kHz audio is clean
4. Resampled 48kHz audio is clean

Saves WAV files at each stage for listening verification.
Run inside the VM: /opt/zoom-bot/venv/bin/python scripts/test-tts-chain.py
"""

import os
import struct
import sys
import wave

# Load .env
for env_path in ("/opt/zoom-bot/.env", "/opt/zoom-bot/zoom_bot/.env"):
    if os.path.isfile(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip("'\"")
                if key and key not in os.environ:
                    os.environ[key] = value

import httpx

ELEVENLABS_API_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # Sarah


def save_wav(path: str, pcm: bytes, sample_rate: int) -> None:
    """Save raw s16le mono PCM as WAV."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    print(f"  Saved {path} ({len(pcm)} bytes, {sample_rate}Hz, {len(pcm)/2/sample_rate:.2f}s)")


def resample_22k_to_32k(pcm_22k: bytes) -> bytes:
    """Resample 22050Hz to 32000Hz via linear interpolation."""
    n = len(pcm_22k) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm_22k)
    ratio = 32000 / 22050
    out_len = int(n * ratio)
    result = []
    for i in range(out_len):
        src_idx = i / ratio
        idx = int(src_idx)
        frac = src_idx - idx
        if idx + 1 < n:
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[idx] if idx < n else 0
        result.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(result)}h", *result)


def resample_32k_to_48k(pcm_32k: bytes) -> bytes:
    """Resample 32kHz to 48kHz via linear interpolation (3:2 ratio)."""
    n = len(pcm_32k) // 2
    if n == 0:
        return b""
    samples = struct.unpack(f"<{n}h", pcm_32k)
    ratio = 48000 / 32000  # 1.5
    out_len = int(n * ratio)
    result = []
    for i in range(out_len):
        src_idx = i / ratio
        idx = int(src_idx)
        frac = src_idx - idx
        if idx + 1 < n:
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[idx] if idx < n else 0
        result.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(result)}h", *result)


def analyze_pcm(pcm: bytes, label: str) -> None:
    """Print basic stats about PCM audio."""
    n = len(pcm) // 2
    if n == 0:
        print(f"  {label}: EMPTY")
        return
    samples = struct.unpack(f"<{n}h", pcm)
    peak = max(abs(s) for s in samples)
    rms = (sum(s * s for s in samples) / n) ** 0.5
    # Check for MP3 header
    is_mp3 = pcm[:3] == b"ID3" or (len(pcm) >= 2 and pcm[0] == 0xFF and (pcm[1] & 0xE0) == 0xE0)
    print(f"  {label}: {n} samples, peak={peak}, RMS={rms:.0f}, "
          f"{'LOOKS LIKE MP3!' if is_mp3 else 'looks like PCM'}")


def main():
    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set")
        sys.exit(1)

    print("=" * 60)
    print("TTS Audio Chain Test")
    print("=" * 60)

    text = "Hello! I'm your AI assistant. I've joined this meeting to help out. How can I assist you today?"

    # Step 1: Call ElevenLabs with output_format as query param
    print("\n1. Calling ElevenLabs API (output_format=pcm_22050 as query param)...")
    with httpx.Client(timeout=30) as client:
        resp = client.post(
            f"{ELEVENLABS_API_URL}/{ELEVENLABS_VOICE_ID}?output_format=pcm_22050",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
            },
            json={
                "text": text,
                "model_id": "eleven_turbo_v2_5",
                "voice_settings": {
                    "stability": 0.3,
                    "similarity_boost": 0.75,
                    "style": 0.5,
                    "use_speaker_boost": True,
                },
            },
        )
        print(f"  Status: {resp.status_code}")
        print(f"  Content-Type: {resp.headers.get('content-type', 'unknown')}")
        print(f"  Content-Length: {len(resp.content)} bytes")

        if resp.status_code != 200:
            print(f"  ERROR: {resp.text[:200]}")
            sys.exit(1)

        raw_pcm = resp.content

    # Check if it's actually PCM or MP3
    if raw_pcm[:3] == b"ID3" or (len(raw_pcm) >= 2 and raw_pcm[0] == 0xFF and (raw_pcm[1] & 0xE0) == 0xE0):
        print("  FAILURE: Response is MP3, not PCM! The fix didn't work.")
        sys.exit(1)
    else:
        print("  SUCCESS: Response appears to be raw PCM (no MP3 header)")

    # Step 2: Analyze and save 22050Hz
    print("\n2. Raw 22050Hz PCM:")
    analyze_pcm(raw_pcm, "22050Hz")
    save_wav("/tmp/tts-test-22050.wav", raw_pcm, 22050)

    # Step 3: Resample to 32kHz (as audio_pipeline.py does)
    print("\n3. Resampling 22050Hz -> 32000Hz:")
    pcm_32k = resample_22k_to_32k(raw_pcm)
    analyze_pcm(pcm_32k, "32000Hz")
    save_wav("/tmp/tts-test-32000.wav", pcm_32k, 32000)

    # Step 4: Resample to 48kHz (as chrome_bot.py does)
    print("\n4. Resampling 32000Hz -> 48000Hz:")
    pcm_48k = resample_32k_to_48k(pcm_32k)
    analyze_pcm(pcm_48k, "48000Hz")
    save_wav("/tmp/tts-test-48000.wav", pcm_48k, 48000)

    # Step 5: Summary
    duration_22k = len(raw_pcm) / 2 / 22050
    duration_48k = len(pcm_48k) / 2 / 48000
    print(f"\n5. Summary:")
    print(f"  Original duration: {duration_22k:.2f}s")
    print(f"  Final duration:    {duration_48k:.2f}s")
    print(f"  Duration ratio:    {duration_48k/duration_22k:.4f} (should be ~1.0)")
    print(f"\n  WAV files saved to /tmp/tts-test-*.wav")
    print(f"  Copy to Mac and play: scp zoom-bot.orb.local:/tmp/tts-test-*.wav /tmp/")
    print("=" * 60)


if __name__ == "__main__":
    main()
