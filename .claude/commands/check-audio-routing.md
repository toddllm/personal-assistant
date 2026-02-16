# Check Audio Routing

Verify that all audio routing services, virtual drivers, and device states match the planned architecture in `docs/audio-routing-architecture.md`.

## Instructions

1. Run the health check script:

```bash
bash scripts/check-audio-routing.sh
```

2. Report the results to the user, highlighting any FAIL or WARN items.

3. If there are failures, explain what's wrong and suggest fixes based on these common issues:

### Bose in HFP mode (muddy output)
Something opened the Bose QC45 mic input, forcing Bluetooth to 16kHz mono.
- Check: Is Bose mic enabled in `data/mic-settings.json`? Must be `{"enabled": false, "volume": 0}`
- Check: Is the C `driver/build/mic-forward` binary running? It may default to Bose mic. Kill it.
- Check: Is `audio-assist` capturing from bose-mic? Check `curl http://127.0.0.1:8787/v1/capture/readiness`
- Fix: Disconnect/reconnect Bluetooth: `blueutil --disconnect AC:BF:71:69:17:48 && sleep 3 && blueutil --connect AC:BF:71:69:17:48`

### Both C and Python mic-forward running
They'll both write to CaptureMic 2ch, doubling the audio signal.
- Fix: Kill the C binary: `kill $(pgrep -f 'driver/build/mic-forward')`
- The service script should start only the Python version for Tauri app integration.

### mic-forward not running
No audio flows from physical mics to CaptureMic 2ch.
- Fix: `PYTHONPATH=src nohup .venv/bin/python scripts/mic-forward.py >> data/logs/mic-forward.log 2>&1 &`
- Or: `scripts/audio-assist-service.sh restart`

### audio-forward not running
No audio flows from CaptureAudio 2ch to physical speakers/headphones.
- Fix: `PYTHONPATH=src nohup .venv/bin/python scripts/audio-forward.py >> data/logs/audio-forward.log 2>&1 &`
- Or: `scripts/audio-assist-service.sh restart`

### Virtual drivers not loaded
coreaudiod may need a restart after driver installation.
- Fix: `cd driver && sudo make -f Makefile install && sudo make -f Makefile.mic install`
- This kills coreaudiod; it auto-restarts in ~3s.

### mic-levels.json stale
mic-forward isn't writing live levels. The Tauri app mic meters won't update.
- Check if mic-forward is running and not stuck in a retry loop.
- Check `data/logs/mic-forward.log` for errors.

4. If all checks pass, confirm the routing is healthy and matches the architecture.
