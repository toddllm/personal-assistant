"""Playwright-based Chrome bot — joins Zoom via web client.

Replaces bot_process.py when the Zoom Meeting SDK's audio encoder
fails under Rosetta x86 emulation. Chrome's WebRTC Opus encoder
is battle-tested and works under Rosetta.

Audio flow:
  Inbound (meeting → STT):
    Participants speak → Chrome WebRTC → virtual_output (PA sink)
      → parecord from virtual_output.monitor → Deepgram STT → Groq LLM

  Outbound (TTS → meeting):
    ElevenLabs TTS → PCM (32kHz) → resample to 48kHz → WebSocket → browser JS
      → AudioContext + MediaStreamDestination → replaceTrack on transmitting sender
      → WebRTC → participants hear AI

Uses --use-fake-device-for-media-stream to bypass Chrome's WebRTC Audio
Processing Module. The fake device establishes WebRTC audio transmission.
After joining, we find the actual transmitting sender (bytesSent > 0) and
replace its track with a Web Audio MediaStreamDestination fed via WebSocket.

Usage (internal — called by main.py):
    python -m zoom_bot.chrome_bot --meeting-id 89105007950 --passcode 857908

State file: /tmp/zoom-bot-state.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import struct
import subprocess
import sys
import time

logger = logging.getLogger(__name__)

STATE_FILE = "/tmp/zoom-bot-state.json"
WS_PORT = 8791


def _resample_32k_to_48k(pcm_32k: bytes) -> bytes:
    """Resample 32kHz s16le mono PCM to 48kHz via linear interpolation (3:2 ratio)."""
    n_samples = len(pcm_32k) // 2
    if n_samples == 0:
        return b""
    samples = struct.unpack(f"<{n_samples}h", pcm_32k)
    ratio = 48000 / 32000  # 1.5
    out_len = int(n_samples * ratio)
    result = []
    for i in range(out_len):
        src_idx = i / ratio
        idx = int(src_idx)
        frac = src_idx - idx
        if idx + 1 < n_samples:
            val = samples[idx] * (1 - frac) + samples[idx + 1] * frac
        else:
            val = samples[min(idx, n_samples - 1)]
        result.append(max(-32768, min(32767, int(val))))
    return struct.pack(f"<{len(result)}h", *result)


class AudioWebSocketServer:
    """WebSocket server that sends TTS PCM audio to the browser page.

    The browser connects to ws://127.0.0.1:{port} and receives binary
    messages containing s16le mono 48kHz PCM audio chunks.
    """

    def __init__(self, port: int = WS_PORT):
        self._port = port
        self._clients: set = set()
        self._server = None

    async def start(self):
        import websockets

        # Suppress noisy 426 errors from Chrome's service worker probing
        logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

        self._server = await websockets.serve(
            self._handler, "127.0.0.1", self._port,
        )
        logger.info("Audio WebSocket server listening on ws://127.0.0.1:%d", self._port)

    async def _handler(self, ws):
        self._clients.add(ws)
        logger.info("Audio WS client connected (total: %d)", len(self._clients))
        try:
            async for _msg in ws:
                pass  # client only receives, doesn't send meaningful data
        except Exception:
            pass
        finally:
            self._clients.discard(ws)
            logger.info("Audio WS client disconnected (total: %d)", len(self._clients))

    async def send_audio(self, pcm_48k_s16le: bytes):
        """Send PCM audio to all connected browser clients.

        Sends in 20ms chunks (960 samples * 2 bytes = 1920 bytes) for
        smooth streaming. The browser schedules each chunk for gapless playback.
        """
        CHUNK_BYTES = 1920  # 20ms at 48kHz mono s16le
        dead = []
        for ws in list(self._clients):
            try:
                for i in range(0, len(pcm_48k_s16le), CHUNK_BYTES):
                    chunk = pcm_48k_s16le[i:i + CHUNK_BYTES]
                    await ws.send(chunk)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    @property
    def has_clients(self) -> bool:
        return len(self._clients) > 0

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None


def _write_state(state: str, meeting_id: str | None = None, error: str | None = None):
    """Write current state to the state file (atomic)."""
    data = {
        "state": state,
        "meeting_id": meeting_id,
        "pid": os.getpid(),
        "timestamp": time.time(),
        "error": error,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE_FILE)


def _kill_orphan_processes():
    """Kill any lingering Chromium/Xvfb processes spawned by this bot."""
    for pattern in ["chromium", "chrome", "Xvfb"]:
        try:
            subprocess.run(
                ["pkill", "-f", pattern],
                capture_output=True,
                timeout=5,
            )
        except Exception:
            pass
    logger.info("Orphan process cleanup complete")


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser()
    parser.add_argument("--meeting-id", required=True)
    parser.add_argument("--passcode", default="")
    parser.add_argument("--max-duration", type=int, default=180)
    parser.add_argument("--prompt", default="")
    args = parser.parse_args()

    # Load env
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

    _write_state("joining", args.meeting_id)

    # ---- PulseAudio setup ----
    _ensure_pulseaudio()

    # Log full PA state for diagnostics
    for pa_type in ["sinks", "sources", "source-outputs"]:
        result = subprocess.run(
            ["pactl", "list", pa_type, "short"],
            capture_output=True, text=True,
        )
        logger.info("PA %s: %s", pa_type, result.stdout.strip())

    # ---- Audio WebSocket server (sends TTS audio to browser JS) ----
    audio_ws = AudioWebSocketServer(port=WS_PORT)
    await audio_ws.start()
    loop = asyncio.get_event_loop()

    # ---- Audio pipeline (provider-pluggable) ----
    from zoom_bot.audio_pipeline import AudioPipeline
    from zoom_bot.bot_config import ZoomBotSettings
    from zoom_bot.provider_factory import create_providers

    settings = ZoomBotSettings()
    stt, llm, tts = create_providers(settings)

    _tts_debug_count = 0

    def on_tts_audio(pcm_data: bytes):
        """Send TTS audio to browser via WebSocket → Web Audio → WebRTC."""
        nonlocal _tts_debug_count
        logger.info("Sending %d bytes TTS audio via WebSocket (32kHz input)", len(pcm_data))
        # pcm_data is 32kHz s16le mono; resample to 48kHz for Web Audio API
        pcm_48k = _resample_32k_to_48k(pcm_data)
        logger.info("Resampled to %d bytes at 48kHz", len(pcm_48k))

        # Debug: save first TTS audio to WAV for verification
        _tts_debug_count += 1
        if _tts_debug_count <= 2:
            _save_debug_wav(f"/tmp/debug-tts-{_tts_debug_count}-32k.wav", pcm_data, 32000)
            _save_debug_wav(f"/tmp/debug-tts-{_tts_debug_count}-48k.wav", pcm_48k, 48000)
            logger.info("Saved debug WAV files for TTS #%d", _tts_debug_count)

        if audio_ws.has_clients:
            asyncio.run_coroutine_threadsafe(audio_ws.send_audio(pcm_48k), loop)
        else:
            logger.warning("No WebSocket clients connected — TTS audio dropped")

    pipeline = AudioPipeline(
        stt=stt,
        llm=llm,
        tts=tts,
        system_prompt=args.prompt or settings.system_prompt,
        on_tts_audio=on_tts_audio,
        debounce_seconds=settings.debounce_seconds,
        max_conversation_turns=settings.max_conversation_turns,
        llm_max_tokens=settings.llm_max_tokens,
        llm_temperature=settings.llm_temperature,
    )
    await pipeline.start()

    # ---- Launch parecord to capture meeting audio from PulseAudio ----
    parecord_proc = _start_parecord(pipeline)

    # ---- Start Xvfb for virtual display (needed for Chrome audio device access) ----
    xvfb_proc = _start_xvfb()

    # ---- Generate primer WAV file for Chrome's fake audio capture ----
    # Chrome needs a real audio file to start transmitting via WebRTC.
    # We generate a 10s low-volume 440Hz tone as a primer. Once WebRTC
    # is established and transmitting, we replace the track with our
    # WebSocket-fed MediaStreamDestination.
    primer_wav_path = "/tmp/bot-primer.wav"
    _generate_primer_wav(primer_wav_path, duration_s=120, sample_rate=48000)
    logger.info("Generated primer WAV: %s", primer_wav_path)

    # ---- Generate black frame Y4M for Chrome's fake video capture ----
    # Without this, --use-fake-device-for-media-stream shows a green
    # spinning triangle animation as the video tile. This replaces it
    # with a static black frame.
    black_video_path = "/tmp/bot-black-video.y4m"
    _generate_black_y4m(black_video_path)
    logger.info("Generated black video: %s", black_video_path)

    # ---- Launch Chromium via Playwright ----
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        _write_state("error", args.meeting_id, "playwright not installed")
        logger.error("playwright not installed — run: pip install playwright && playwright install chromium --with-deps")
        sys.exit(1)

    browser = None
    page = None
    start_time = None

    try:
        async with async_playwright() as pw:
            logger.info("Launching Chromium (non-headless with Xvfb)...")
            browser = await pw.chromium.launch(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--use-fake-device-for-media-stream",   # bypass APM
                    f"--use-file-for-fake-audio-capture={primer_wav_path}",  # primer to start WebRTC
                    f"--use-file-for-fake-video-capture={black_video_path}",  # black frame instead of green animation
                    "--use-fake-ui-for-media-stream",       # auto-grant mic/camera permissions
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )

            context = await browser.new_context(
                permissions=["microphone", "camera"],
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
            )

            # Init script: track RTCPeerConnections + set up WebSocket audio receiver.
            # Audio injection happens post-join via _replace_transmitting_track().
            await context.add_init_script("""
(() => {
  // ---- Track all RTCPeerConnections + audio senders ----
  window.__rtcPCs = [];
  window.__audioSenderCount = 0;
  const OrigPC = window.RTCPeerConnection;
  window.RTCPeerConnection = function(...args) {
    const pc = new OrigPC(...args);
    const idx = window.__rtcPCs.length;
    window.__rtcPCs.push(pc);
    console.log('[bot] RTCPeerConnection #' + idx + ' created, total:', window.__rtcPCs.length);

    // Monitor addTrack calls to know when audio senders are added
    const origAddTrack = pc.addTrack.bind(pc);
    pc.addTrack = function(track, ...streams) {
      const sender = origAddTrack(track, ...streams);
      if (track && track.kind === 'audio') {
        window.__audioSenderCount++;
        console.log('[bot] PC#' + idx + ' addTrack(audio) — total audio senders: ' + window.__audioSenderCount);
      }
      return sender;
    };

    return pc;
  };
  window.RTCPeerConnection.prototype = OrigPC.prototype;
  Object.keys(OrigPC).forEach(k => { window.RTCPeerConnection[k] = OrigPC[k]; });

  // ---- Audio state (populated by post-join track replacement) ----
  window.__botAudio = {
    ctx: null,           // AudioContext (48kHz)
    dest: null,          // MediaStreamDestination — our audio goes here
    processor: null,     // ScriptProcessorNode for continuous audio output
    ws: null,            // WebSocket to Python server
    connected: false,
    playing: false,
    samplesReceived: 0,
    chunksPlayed: 0,
    trackReplaced: false,
    replacedSenderPC: -1,
    replacedSenderIdx: -1,
  };

  // ---- WebSocket audio receiver ----
  // Connects to Python's AudioWebSocketServer, receives s16le 48kHz PCM,
  // feeds into a ScriptProcessorNode for continuous output to MediaStreamDestination.
  window.__botStartAudioWS = function() {
    const state = window.__botAudio;
    if (state.ws) return;  // already connected

    const ws = new WebSocket('ws://127.0.0.1:""" + str(WS_PORT) + """');
    ws.binaryType = 'arraybuffer';
    state.ws = ws;

    // Ring buffer for audio samples (Float32)
    // 10 seconds of buffer at 48kHz
    const RING_SIZE = 48000 * 10;
    const ringBuf = new Float32Array(RING_SIZE);
    let writePos = 0;
    let readPos = 0;
    let bufferedSamples = 0;

    function ringWrite(float32Array) {
      for (let i = 0; i < float32Array.length; i++) {
        ringBuf[writePos] = float32Array[i];
        writePos = (writePos + 1) % RING_SIZE;
      }
      bufferedSamples = Math.min(bufferedSamples + float32Array.length, RING_SIZE);
    }

    function ringRead(output) {
      for (let i = 0; i < output.length; i++) {
        if (bufferedSamples > 0) {
          output[i] = ringBuf[readPos];
          readPos = (readPos + 1) % RING_SIZE;
          bufferedSamples--;
        } else {
          output[i] = 0;  // silence when buffer empty
        }
      }
    }

    // Start ScriptProcessorNode when AudioContext + dest are available
    function startProcessor() {
      if (!state.ctx || !state.dest || state.processor) return;
      // 4096 samples per callback at 48kHz = ~85ms per callback
      const proc = state.ctx.createScriptProcessor(4096, 0, 1);
      proc.onaudioprocess = (e) => {
        const out = e.outputBuffer.getChannelData(0);
        ringRead(out);
        state.playing = bufferedSamples > 0;
      };
      proc.connect(state.dest);
      state.processor = proc;
      console.log('[bot] ScriptProcessorNode started (4096 samples/callback)');
    }

    // Expose globally so _replace_transmitting_track can trigger it
    window.__botStartProcessor = startProcessor;

    ws.onopen = () => {
      state.connected = true;
      console.log('[bot] Audio WebSocket connected');
      startProcessor();
    };

    ws.onmessage = (e) => {
      if (!state.dest) return;

      // Ensure AudioContext is running
      if (state.ctx && state.ctx.state === 'suspended') {
        state.ctx.resume();
      }

      // Convert Int16 → Float32 and write to ring buffer
      const int16 = new Int16Array(e.data);
      state.samplesReceived += int16.length;
      const floats = new Float32Array(int16.length);
      for (let i = 0; i < int16.length; i++) {
        floats[i] = int16[i] / 32768.0;
      }
      ringWrite(floats);
      state.chunksPlayed++;

      // Start processor if not yet started
      startProcessor();
    };

    ws.onclose = () => {
      state.connected = false;
      state.ws = null;
      console.log('[bot] Audio WebSocket closed, reconnecting in 2s...');
      setTimeout(window.__botStartAudioWS, 2000);
    };

    ws.onerror = (e) => {
      console.log('[bot] Audio WebSocket error:', e.type);
    };
  };
})();
""")

            page = await context.new_page()

            # Capture all browser console output for diagnostics
            page.on("console", lambda msg: logger.info("[chrome] %s", msg.text))

            # Join the meeting
            joined = await _join_zoom_meeting(page, args.meeting_id, args.passcode)
            if not joined:
                _write_state("error", args.meeting_id, "Failed to join Zoom web meeting")
                return

            start_time = time.time()
            _write_state("in_meeting", args.meeting_id)
            logger.info("Successfully joined meeting %s via Chrome", args.meeting_id)

            # Turn off video — even with black Y4M, clicking Stop Video
            # removes the video tile entirely so bot shows as audio-only.
            for video_sel in [
                'button[aria-label="Stop Video"]',
                'button:has-text("Stop Video")',
                '.send-video-container button',
            ]:
                try:
                    btn = page.locator(video_sel).first
                    if await btn.is_visible(timeout=2000):
                        await btn.click()
                        logger.info("Clicked Stop Video button: %s", video_sel)
                        break
                except Exception:
                    pass

            # Start the browser-side WebSocket connection to our audio server
            await page.evaluate("window.__botStartAudioWS()")
            logger.info("Browser WebSocket audio receiver started")

            # ---- TARGETED POST-JOIN TRACK REPLACEMENT ----
            # IMPORTANT: Don't replace the track until we confirm bytesSent > 0.
            # The primer WAV makes Chrome transmit audio. If we replace the track
            # before Chrome starts transmitting, bytesSent will never increase
            # (our silent MediaStreamDestination triggers DTX — no packets sent).
            track_replaced = False

            for attempt in range(30):  # up to ~60s
                # Click audio buttons each attempt (they may appear/reappear)
                for audio_sel in [
                    'button:has-text("Join Audio by Computer")',
                    'button:has-text("Join Audio")',
                    'button:has-text("Computer Audio")',
                    '.join-audio-by-voip',
                ]:
                    try:
                        btn = page.locator(audio_sel).first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            logger.info("Clicked audio button: '%s' (attempt %d)", audio_sel, attempt)
                            await asyncio.sleep(1)
                    except Exception:
                        pass

                await asyncio.sleep(2)

                # Phase 1: SCAN ONLY — don't replace yet, just check bytesSent
                scan_result = await _scan_webrtc_senders(page)
                logger.info("Scan attempt %d: %s", attempt, scan_result)

                try:
                    scan_data = json.loads(scan_result)
                    best_pc = scan_data.get("bestPC", -1)
                    best_bytes = scan_data.get("bestBytesSent", 0)

                    if best_bytes > 0:
                        # Found transmitting PC — NOW replace the track
                        logger.info("Found transmitting PC#%d (bytesSent=%d), replacing track...",
                                    best_pc, best_bytes)
                        replace_result = await _replace_transmitting_track(page)
                        result_data = json.loads(replace_result)
                        if result_data.get("success"):
                            track_replaced = True
                            logger.info("Track replaced on PC#%d (bytesSent=%d)",
                                        result_data.get("pcIndex"), result_data.get("bytesSentBefore"))
                            break
                    elif attempt >= 20:
                        # Fallback: no PC ever got bytesSent > 0 — just replace
                        # on any PC with a live audio sender
                        logger.warning("No transmitting PC after %d attempts, using fallback", attempt)
                        replace_result = await _replace_transmitting_track(page)
                        result_data = json.loads(replace_result)
                        if result_data.get("success"):
                            track_replaced = True
                            logger.warning("Fallback track replacement on PC#%d",
                                           result_data.get("pcIndex"))
                            break
                except (json.JSONDecodeError, TypeError):
                    pass

            if not track_replaced:
                logger.error("Failed to replace audio track after 30 attempts")

            # Phase 3: Verify replacement — check bytesSent is growing
            if track_replaced:
                await asyncio.sleep(3)
                try:
                    stats1 = await _get_webrtc_stats(page)
                    await asyncio.sleep(2)
                    stats2 = await _get_webrtc_stats(page)
                    logger.info("Post-replacement verification: t0=%s, t+2s=%s", stats1, stats2)
                except Exception:
                    pass

            # Send a greeting through TTS so the user knows the bot is live
            await asyncio.sleep(2)
            logger.info("Sending greeting via TTS...")
            await pipeline.send_tts("Hello! I've joined the meeting.")

            # Check WebRTC stats after greeting
            await asyncio.sleep(4)
            try:
                stats = await _get_webrtc_stats(page)
                logger.info("WebRTC stats after greeting: %s", stats)
            except Exception:
                pass


            # ---- Main loop: stay in meeting until max_duration or signal ----
            shutdown = asyncio.Event()

            def _handle_signal():
                logger.info("Received shutdown signal")
                shutdown.set()

            loop = asyncio.get_event_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, _handle_signal)

            last_stats_time = 0

            while not shutdown.is_set():
                # Check max duration
                elapsed = time.time() - start_time
                if elapsed > args.max_duration:
                    logger.info("Max duration %ds reached, leaving", args.max_duration)
                    break

                # Check if still in meeting (page not navigated away)
                try:
                    url = page.url
                    if "zoom.us" not in url and "zoom.com" not in url:
                        logger.info("Page navigated away from Zoom (%s), leaving", url)
                        break
                except Exception:
                    break

                # Periodic WebRTC stats + audio state (every 10 seconds)
                now = time.time()
                if now - last_stats_time >= 10:
                    last_stats_time = now
                    try:
                        stats = await _get_webrtc_stats(page)
                        logger.info("WebRTC stats (t=%ds): %s", int(elapsed), stats)
                    except Exception:
                        pass
                    try:
                        audio_state = await page.evaluate("""
                            () => {
                                const s = window.__botAudio;
                                return JSON.stringify({
                                    trackReplaced: s.trackReplaced,
                                    wsConnected: s.connected,
                                    samplesReceived: s.samplesReceived,
                                    chunksPlayed: s.chunksPlayed,
                                    playing: s.playing,
                                    ctxState: s.ctx ? s.ctx.state : 'none',
                                });
                            }
                        """)
                        logger.info("Audio state (t=%ds): %s", int(elapsed), audio_state)
                    except Exception:
                        pass

                try:
                    await asyncio.wait_for(shutdown.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass

    except Exception:
        logger.exception("Chrome bot error")
        _write_state("error", args.meeting_id, "Chrome bot crashed")
    finally:
        _write_state("leaving", args.meeting_id)

        # 1. Close browser first (tells Chrome to exit cleanly)
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

        # 2. Stop audio capture/display processes
        for proc in [parecord_proc, xvfb_proc]:
            if proc:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()

        # 3. Kill any orphaned Chrome/Xvfb processes from our session
        _kill_orphan_processes()

        await audio_ws.stop()
        await pipeline.stop()

        _write_state("idle")
        logger.info("Chrome bot exiting")


def _save_debug_wav(path: str, pcm_data: bytes, sample_rate: int):
    """Save raw PCM data as a WAV file for debugging."""
    with open(path, "wb") as f:
        bits = 16
        ch = 1
        byte_rate = sample_rate * ch * bits // 8
        block_align = ch * bits // 8
        data_size = len(pcm_data)
        f.write(b"RIFF")
        f.write(struct.pack("<I", data_size + 36))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))
        f.write(struct.pack("<H", ch))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm_data)


def _generate_black_y4m(path: str):
    """Generate a single-frame black Y4M video for Chrome's fake video capture.

    Replaces the default green spinning triangle animation that Chrome
    generates with --use-fake-device-for-media-stream.
    """
    # YUV4MPEG2: 2x2 pixels, 1fps, 4:2:0 chroma subsampling
    header = b"YUV4MPEG2 W2 H2 F1:1 Ip A1:1 C420\n"
    # Y plane: 4 pixels at value 16 (black in limited-range YUV)
    # U/V planes: 1 pixel each at value 128 (neutral chroma)
    frame = b"FRAME\n" + bytes([16] * 4) + bytes([128]) + bytes([128])
    with open(path, "wb") as f:
        f.write(header + frame)


def _generate_primer_wav(path: str, duration_s: int = 10, sample_rate: int = 48000):
    """Generate a WAV file with a low-volume tone for Chrome's fake audio capture.

    This file primes Chrome's WebRTC audio pipeline so that bytesSent > 0
    before we replace the track with our WebSocket-fed MediaStreamDestination.
    """
    n_samples = sample_rate * duration_s
    # Low-volume 440Hz tone (amplitude ~3000 out of 32768 ≈ -20dB)
    samples = [
        int(3000 * math.sin(2 * math.pi * 440 * i / sample_rate))
        for i in range(n_samples)
    ]
    pcm = struct.pack(f"<{n_samples}h", *samples)

    with open(path, "wb") as f:
        # WAV header
        bits = 16
        ch = 1
        byte_rate = sample_rate * ch * bits // 8
        block_align = ch * bits // 8
        data_size = len(pcm)

        f.write(b"RIFF")
        f.write(struct.pack("<I", data_size + 36))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<I", 16))
        f.write(struct.pack("<H", 1))  # PCM
        f.write(struct.pack("<H", ch))
        f.write(struct.pack("<I", sample_rate))
        f.write(struct.pack("<I", byte_rate))
        f.write(struct.pack("<H", block_align))
        f.write(struct.pack("<H", bits))
        f.write(b"data")
        f.write(struct.pack("<I", data_size))
        f.write(pcm)


async def _scan_webrtc_senders(page) -> str:
    """Scan RTCPeerConnections for audio senders and their bytesSent.

    Does NOT replace any tracks — just reports the current state.
    Returns JSON with bestPC index and bestBytesSent.
    """
    return await page.evaluate("""
    async () => {
        const pcs = window.__rtcPCs || [];
        let bestPC = -1;
        let bestBytesSent = 0;
        let totalAudioSenders = 0;
        const pcSummary = [];

        for (let pcIdx = 0; pcIdx < pcs.length; pcIdx++) {
            const pc = pcs[pcIdx];
            if (pc.signalingState === 'closed') continue;
            try {
                const senders = pc.getSenders();
                const stats = await pc.getStats();
                let pcBytesSent = 0;
                stats.forEach(s => {
                    if (s.type === 'outbound-rtp' && s.kind === 'audio') {
                        pcBytesSent = s.bytesSent || 0;
                    }
                });
                let hasAudio = false;
                for (const sender of senders) {
                    if (sender.track && sender.track.kind === 'audio') {
                        hasAudio = true;
                        totalAudioSenders++;
                    }
                }
                if (hasAudio) {
                    pcSummary.push('PC#' + pcIdx + ':bytes=' + pcBytesSent);
                }
                if (pcBytesSent > bestBytesSent) {
                    bestPC = pcIdx;
                    bestBytesSent = pcBytesSent;
                }
            } catch(e) {}
        }
        return JSON.stringify({
            bestPC: bestPC,
            bestBytesSent: bestBytesSent,
            totalAudioSenders: totalAudioSenders,
            summary: pcSummary.join(', '),
        });
    }
    """)


async def _replace_transmitting_track(page) -> str:
    """Find the actual transmitting audio sender and replace its track.

    Scans ALL tracked RTCPeerConnections for the one with bytesSent > 0
    on an audio outbound-rtp sender. Creates an AudioContext +
    MediaStreamDestination and replaces only that sender's track.

    Returns a JSON string with the result.
    """
    return await page.evaluate("""
    async () => {
        const pcs = window.__rtcPCs || [];
        const state = window.__botAudio;
        const log = [];

        log.push('Scanning ' + pcs.length + ' PCs for transmitting audio sender...');

        let foundPC = -1;
        let foundSenderIdx = -1;
        let foundSender = null;
        let maxBytesSent = 0;

        for (let pcIdx = 0; pcIdx < pcs.length; pcIdx++) {
            const pc = pcs[pcIdx];
            try {
                const senders = pc.getSenders();
                const stats = await pc.getStats();

                // Find outbound audio RTP with bytesSent > 0
                let pcBytesSent = 0;
                stats.forEach(s => {
                    if (s.type === 'outbound-rtp' && s.kind === 'audio') {
                        pcBytesSent = s.bytesSent || 0;
                    }
                });

                log.push('PC#' + pcIdx + ': senders=' + senders.length +
                          ', signalingState=' + pc.signalingState +
                          ', audioBytesSent=' + pcBytesSent);

                // Log all senders' track state for diagnostics
                for (let si = 0; si < senders.length; si++) {
                    const sender = senders[si];
                    const t = sender.track;
                    if (t) {
                        log.push('  sender#' + si + ': kind=' + t.kind +
                                 ' enabled=' + t.enabled + ' muted=' + t.muted +
                                 ' readyState=' + t.readyState);
                    }
                }

                if (pcBytesSent > maxBytesSent) {
                    // Find the audio sender on this PC
                    for (let si = 0; si < senders.length; si++) {
                        const sender = senders[si];
                        if (sender.track && sender.track.kind === 'audio') {
                            foundPC = pcIdx;
                            foundSenderIdx = si;
                            foundSender = sender;
                            maxBytesSent = pcBytesSent;
                            log.push('  -> BEST audio sender: PC#' + pcIdx +
                                     ' sender#' + si + ' bytesSent=' + pcBytesSent);
                        }
                    }
                }
            } catch(e) {
                log.push('PC#' + pcIdx + ': error - ' + e.message);
            }
        }

        // Fallback 1: if no sender has bytesSent > 0, pick any audio sender
        // with a live track on any non-closed PC.
        if (!foundSender) {
            log.push('No sender with bytesSent > 0, trying fallback (live track on any PC)...');
            for (let pcIdx = 0; pcIdx < pcs.length; pcIdx++) {
                const pc = pcs[pcIdx];
                if (pc.signalingState === 'closed') continue;
                try {
                    const senders = pc.getSenders();
                    for (let si = 0; si < senders.length; si++) {
                        const sender = senders[si];
                        if (sender.track && sender.track.kind === 'audio' &&
                            sender.track.readyState === 'live') {
                            foundPC = pcIdx;
                            foundSenderIdx = si;
                            foundSender = sender;
                            log.push('Fallback1: using PC#' + pcIdx + ' sender#' + si +
                                     ' (live track, signalingState=' + pc.signalingState + ')');
                            break;
                        }
                    }
                    if (foundSender) break;
                } catch(e) {}
            }
        }

        // Fallback 2: pick ANY audio sender, even with ended track
        if (!foundSender) {
            log.push('Fallback1 failed, trying any audio sender at all...');
            for (let pcIdx = 0; pcIdx < pcs.length; pcIdx++) {
                const pc = pcs[pcIdx];
                if (pc.signalingState === 'closed') continue;
                try {
                    const senders = pc.getSenders();
                    for (let si = 0; si < senders.length; si++) {
                        const sender = senders[si];
                        if (sender.track && sender.track.kind === 'audio') {
                            foundPC = pcIdx;
                            foundSenderIdx = si;
                            foundSender = sender;
                            log.push('Fallback2: using PC#' + pcIdx + ' sender#' + si +
                                     ' (track.readyState=' + sender.track.readyState + ')');
                            break;
                        }
                    }
                    if (foundSender) break;
                } catch(e) {}
            }
        }

        if (!foundSender) {
            log.push('ERROR: No audio sender found on any PC!');
            return JSON.stringify({ success: false, log: log });
        }

        log.push('Replacing track on PC#' + foundPC + ' sender#' + foundSenderIdx +
                  ' (bytesSent=' + maxBytesSent + ')');

        try {
            // Create AudioContext at 48kHz
            const ctx = new AudioContext({ sampleRate: 48000 });
            log.push('AudioContext created, state=' + ctx.state + ', sampleRate=' + ctx.sampleRate);

            // Resume if suspended (Chrome policy)
            if (ctx.state === 'suspended') {
                await ctx.resume();
                log.push('AudioContext resumed, state=' + ctx.state);
            }

            // Create MediaStreamDestination — our TTS audio goes here
            const dest = ctx.createMediaStreamDestination();
            const newTrack = dest.stream.getAudioTracks()[0];
            log.push('MediaStreamDestination created, track.id=' + newTrack.id +
                      ', track.enabled=' + newTrack.enabled +
                      ', track.readyState=' + newTrack.readyState);

            // Note: ScriptProcessorNode (started by __botStartAudioWS) provides
            // continuous audio output (silence when no TTS, speech when TTS plays).
            // This keeps WebRTC's encoder fed without needing a separate silence generator.

            // Replace the transmitting sender's track
            await foundSender.replaceTrack(newTrack);
            log.push('replaceTrack() succeeded!');

            // Save state for WebSocket audio playback
            state.ctx = ctx;
            state.dest = dest;
            state.trackReplaced = true;
            state.replacedSenderPC = foundPC;
            state.replacedSenderIdx = foundSenderIdx;

            // Trigger ScriptProcessorNode start if WebSocket is already connected
            // (startProcessor checks state.ctx/state.dest/state.processor)
            if (typeof window.__botStartProcessor === 'function') {
                window.__botStartProcessor();
                log.push('Triggered ScriptProcessorNode start');
            }

            return JSON.stringify({
                success: true,
                pcIndex: foundPC,
                senderIndex: foundSenderIdx,
                bytesSentBefore: maxBytesSent,
                log: log,
            });

        } catch(e) {
            log.push('replaceTrack failed: ' + e.message);
            return JSON.stringify({ success: false, error: e.message, log: log });
        }
    }
    """)


async def _get_webrtc_stats(page) -> str:
    """Query WebRTC stats from all tracked RTCPeerConnections."""
    return await page.evaluate("""
    async () => {
        const pcs = window.__rtcPCs || [];
        const results = [];
        for (const pc of pcs) {
            try {
                const stats = await pc.getStats();
                const r = {};
                stats.forEach(s => {
                    if (s.type === 'outbound-rtp' && s.kind === 'audio') {
                        r.outbound = {
                            bytesSent: s.bytesSent,
                            packetsSent: s.packetsSent,
                        };
                    }
                    if (s.type === 'media-source' && s.kind === 'audio') {
                        r.mediaSource = {
                            totalAudioEnergy: s.totalAudioEnergy,
                            audioLevel: s.audioLevel,
                            totalSamplesDuration: s.totalSamplesDuration,
                        };
                    }
                });
                if (Object.keys(r).length > 0) results.push(r);
            } catch(e) {}
        }
        return JSON.stringify(results);
    }
    """)


def _start_xvfb() -> subprocess.Popen | None:
    """Start Xvfb virtual display so Chrome can access audio devices."""
    display = ":99"
    os.environ["DISPLAY"] = display
    try:
        proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x720x24", "-ac"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        logger.info("Xvfb started on %s (PID %d)", display, proc.pid)
        return proc
    except FileNotFoundError:
        logger.warning("Xvfb not found — Chrome may not detect audio devices")
        return None


def _ensure_pulseaudio():
    """Ensure PulseAudio is running with the right modules."""
    try:
        subprocess.run(["pulseaudio", "--check"], check=True, capture_output=True)
        logger.info("PulseAudio already running")
    except (subprocess.CalledProcessError, FileNotFoundError):
        logger.info("Starting PulseAudio...")
        subprocess.Popen(
            ["pulseaudio", "--start", "--daemonize"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)

    # Null sink for meeting output (Chrome plays here; monitor used for STT capture)
    _pactl_load_module_if_missing(
        "module-null-sink",
        'sink_name=virtual_output sink_properties=device.description="MeetingOutput"',
        sink_name="virtual_output",
    )
    # Note: tts_sink and chrome_mic no longer needed — TTS audio is injected
    # directly into WebRTC via WebSocket + Web Audio API, bypassing PulseAudio.

    subprocess.run(["pactl", "set-default-sink", "virtual_output"], capture_output=True)
    logger.info("PulseAudio configured: sink=virtual_output (STT capture only; TTS via WebSocket→WebRTC)")


def _pactl_load_module_if_missing(module: str, args: str, sink_name: str = ""):
    """Load a PulseAudio module if not already loaded.

    When sink_name is provided, checks for that specific sink rather than
    just the module name (needed when loading multiple module-null-sink instances).
    """
    result = subprocess.run(
        ["pactl", "list", "modules", "short"],
        capture_output=True, text=True,
    )
    listing = result.stdout or ""
    if sink_name:
        if sink_name in listing:
            return
    elif module in listing:
        return
    cmd = ["pactl", "load-module", module] + args.split()
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error("pactl load-module failed: %s — stderr: %s", cmd, result.stderr.strip())
    else:
        logger.info("Loaded PA module: %s (id=%s)", module, result.stdout.strip())


def _start_parecord(pipeline) -> subprocess.Popen | None:
    """Start parecord to capture meeting audio from virtual_output.monitor.

    Streams 16kHz mono s16le PCM to the pipeline's feed_audio method.
    """
    try:
        proc = subprocess.Popen(
            [
                "parecord",
                "--device=virtual_output.monitor",
                "--format=s16le",
                "--rate=16000",
                "--channels=1",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        logger.info("parecord started (PID %d) — capturing meeting audio", proc.pid)

        import threading

        def _reader():
            """Read parecord output and feed to pipeline."""
            chunk_size = 3200  # 100ms at 16kHz mono 16-bit
            while True:
                data = proc.stdout.read(chunk_size)
                if not data:
                    break
                # Pipeline expects 32kHz; upsample by duplicating samples
                samples = struct.unpack(f"<{len(data) // 2}h", data)
                upsampled = []
                for s in samples:
                    upsampled.extend([s, s])
                pcm_32k = struct.pack(f"<{len(upsampled)}h", *upsampled)
                pipeline.feed_audio(pcm_32k)

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        return proc

    except FileNotFoundError:
        logger.warning("parecord not found — meeting audio capture disabled")
        return None


async def _join_zoom_meeting(page, meeting_id: str, passcode: str) -> bool:
    """Join a Zoom meeting via the web client.

    Returns True if successfully joined.
    """
    meeting_id_clean = meeting_id.replace(" ", "").replace("-", "")
    url = f"https://app.zoom.us/wc/join/{meeting_id_clean}"

    logger.info("Navigating to %s", url)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        logger.exception("Failed to navigate to Zoom web client")
        return False

    await asyncio.sleep(3)

    # Debug screenshot after initial page load
    try:
        await page.screenshot(path="/tmp/zoom-debug-01-pageload.png")
        logger.info("DEBUG screenshot: /tmp/zoom-debug-01-pageload.png (url=%s)", page.url)
    except Exception:
        pass

    # Handle cookie/consent banners
    for sel in [
        'button:has-text("Accept")',
        'button:has-text("Got it")',
        '#onetrust-accept-btn-handler',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                await asyncio.sleep(0.5)
        except Exception:
            pass

    # Fill passcode and name — Zoom may show them on a single combined form
    # or as separate steps. Try the combined form first, then individual fields.
    passcode_filled = False
    name_filled = False

    # Passcode field (multiple selector strategies)
    if passcode:
        for pwd_sel in [
            '#input-for-pwd',
            'input[type="password"]',
            'label:has-text("Passcode") + input',
            'label:has-text("Meeting Passcode") ~ input',
        ]:
            try:
                pwd_input = page.locator(pwd_sel).first
                if await pwd_input.is_visible(timeout=2000):
                    await pwd_input.fill(passcode)
                    passcode_filled = True
                    logger.info("Filled passcode via selector: %s", pwd_sel)
                    break
            except Exception:
                continue
        if not passcode_filled:
            # Try Playwright's get_by_label for accessibility-based matching
            try:
                pwd_input = page.get_by_label("Passcode", exact=False)
                if await pwd_input.is_visible(timeout=2000):
                    await pwd_input.fill(passcode)
                    passcode_filled = True
                    logger.info("Filled passcode via get_by_label")
            except Exception:
                pass
        if not passcode_filled:
            logger.warning("Could not find passcode field")

    # Name field (multiple selector strategies)
    for name_sel in [
        '#input-for-name',
        'input[placeholder*="name" i]',
        'label:has-text("Your Name") + input',
        'label:has-text("Your Name") ~ input',
    ]:
        try:
            name_input = page.locator(name_sel).first
            if await name_input.is_visible(timeout=2000):
                await name_input.fill("AI Assistant")
                name_filled = True
                logger.info("Filled name via selector: %s", name_sel)
                break
        except Exception:
            continue
    if not name_filled:
        try:
            name_input = page.get_by_label("Your Name", exact=False)
            if await name_input.is_visible(timeout=2000):
                await name_input.fill("AI Assistant")
                name_filled = True
                logger.info("Filled name via get_by_label")
        except Exception:
            pass
    if not name_filled:
        logger.warning("Could not find name field")

    await asyncio.sleep(0.5)

    # Click Join / Submit button
    join_clicked = False
    for btn_sel in [
        'button:has-text("Join")',
        'button.zm-btn-legacy',
        'button[type="submit"]',
        'button:has-text("Submit")',
    ]:
        try:
            btn = page.locator(btn_sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                join_clicked = True
                logger.info("Clicked join button: %s", btn_sel)
                break
        except Exception:
            continue
    if not join_clicked:
        logger.warning("Could not find join button")
    await asyncio.sleep(3)

    # Debug screenshot after name/passcode entry
    try:
        await page.screenshot(path="/tmp/zoom-debug-02-afterjoin.png")
        logger.info("DEBUG screenshot: /tmp/zoom-debug-02-afterjoin.png (url=%s)", page.url)
    except Exception:
        pass

    # Wait for meeting to load (look for typical in-meeting elements)
    logger.info("Waiting for meeting to load...")
    in_meeting = False

    for attempt in range(30):
        await asyncio.sleep(2)

        # Check for error messages first
        for err_sel in [
            '.error-message',
            ':has-text("meeting has ended")',
            ':has-text("meeting ID is not valid")',
            ':has-text("removed from this meeting")',
        ]:
            try:
                el = page.locator(err_sel).first
                if await el.is_visible(timeout=500):
                    text = await el.text_content()
                    logger.error("Meeting error: %s", text)
                    return False
            except Exception:
                pass

        # Periodic debug screenshots during wait
        if attempt % 10 == 5:
            try:
                await page.screenshot(path=f"/tmp/zoom-debug-wait-{attempt}.png")
                logger.info("DEBUG screenshot: /tmp/zoom-debug-wait-%d.png (url=%s)", attempt, page.url)
            except Exception:
                pass

        # Check for in-meeting indicators
        if not in_meeting:
            for indicator in [
                '.meeting-app',
                '#wc-container-right',
                '.meeting-info-container',
                '#foot-bar',
                '.footer__inner',
            ]:
                try:
                    el = page.locator(indicator).first
                    if await el.is_visible(timeout=500):
                        logger.info("In-meeting indicator found: %s", indicator)
                        in_meeting = True
                        break
                except Exception:
                    pass

        # Always try to click audio/consent buttons (they appear after meeting loads)
        for audio_sel in [
            'button:has-text("Join Audio by Computer")',
            'button:has-text("Join Audio")',
            'button:has-text("Computer Audio")',
            '.join-audio-by-voip',
        ]:
            try:
                btn = page.locator(audio_sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    logger.info("Clicked audio button: '%s'", audio_sel)
                    await asyncio.sleep(1)
            except Exception:
                pass

        # Accept recording consent / popups
        for consent_sel in [
            'button:has-text("Got it")',
            'button:has-text("I Agree")',
            'button:has-text("OK")',
        ]:
            try:
                btn = page.locator(consent_sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("Clicked consent button: '%s'", consent_sel)
                    await asyncio.sleep(0.5)
            except Exception:
                pass

        # Once in meeting, keep scanning for audio buttons for a few more cycles then return
        if in_meeting and attempt >= 3:
            # Take a screenshot for debugging
            try:
                await page.screenshot(path="/tmp/zoom-meeting-state.png")
                logger.info("Screenshot saved to /tmp/zoom-meeting-state.png")
            except Exception:
                pass
            return True

    if in_meeting:
        return True

    # Debug screenshot before fallback
    try:
        await page.screenshot(path="/tmp/zoom-debug-03-fallback.png")
        logger.info("DEBUG screenshot: /tmp/zoom-debug-03-fallback.png (url=%s)", page.url)
    except Exception:
        pass

    # Fallback: check if page has meaningful content
    try:
        body = await page.locator("body").text_content()
        if body and len(body) > 100:
            logger.info("Meeting page loaded (no indicator matched, but page has content)")
            return True
    except Exception:
        pass

    logger.warning("Timed out waiting for meeting to load")
    return False


if __name__ == "__main__":
    asyncio.run(main())
