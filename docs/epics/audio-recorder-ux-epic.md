# EPIC: Audio Recorder UX

## Summary
Evolve the in-app audio recorder from a minimal start/stop button into a fully usable recording tool with device selection, file management, visual feedback, and integration with the existing capture and transcription stack.

## Why
The recorder today captures audio from a single hardcoded device with no way to choose inputs, manage files, or see meaningful feedback. For the recorder to be a daily-use tool (meeting capture, voice notes, ambient recording), it needs to be as intuitive as pressing record on a phone but with the power of the full audio routing stack underneath.

## First Principles
A recorder has three jobs: **capture the right audio**, **show the user it's working**, and **make the output easy to find and use**. Every issue below maps to one of these.

## Success Metrics
- User can start a recording from any available input device in < 3 clicks.
- Active recording is visually obvious from any panel in the app.
- Completed recordings can be found, played, renamed, and deleted without leaving the app.
- No orphaned ffmpeg processes after app restart or crash.

## Scope
- Tauri app recorder UI and Rust backend commands.
- File management within the app.
- Integration hooks to existing audio-assist transcription.

## Out of Scope
- Cloud upload / sync of recordings.
- Real-time streaming to external services.
- Audio editing (trim, splice, effects).

---

## Issues

### 1. Device selector dropdown
**Job: Capture the right audio**

Add a dropdown next to the Record button that lists available AVFoundation input devices. Default to the current system default but let the user pick any device (CaptureMic, CaptureAudio, BlackHole, MacBook Pro Microphone, etc.). Persist the last-used device choice.

- [ ] Add Tauri command `list_audio_input_devices` (parse `ffmpeg -f avfoundation -list_devices true`)
- [ ] Add dropdown to recording row, populated on panel load
- [ ] Pass selected device to `start_recording`
- [ ] Persist last-used device in local storage

### 2. Multi-source recording
**Job: Capture the right audio**

Record from multiple input devices simultaneously into separate tracks or a single mixed file. Primary use case: capture mic + system audio together for meeting recordings.

- [ ] Support starting multiple ffmpeg instances with different inputs
- [ ] Option A: separate WAV files per source, named with device suffix
- [ ] Option B: merge into a single multi-channel or mixed file via ffmpeg filter
- [ ] UI to select "Mic + System" as a combined preset

### 3. Output format options
**Job: Capture the right audio**

WAV files grow ~5.6 MB/min at 48kHz mono. A 1-hour meeting is ~340 MB. Add compressed format options.

- [ ] Add format selector: WAV (lossless), FLAC (lossless compressed ~60% smaller), MP3/AAC (lossy, ~10x smaller)
- [ ] Pass format/codec args to ffmpeg command
- [ ] Default to FLAC for space efficiency with no quality loss
- [ ] Show estimated file size rate in UI

### 4. Recording state indicator in header/tray
**Job: Show it's working**

When scrolled away from the audio panel, there's no visual cue that a recording is active. A red dot should be visible at all times.

- [ ] Add a recording indicator dot to the app header bar (next to the clock)
- [ ] Update tray icon to show recording state (red dot overlay or alternate icon)
- [ ] Click header indicator to scroll to / focus the recording panel

### 5. Smoother level meter
**Job: Show it's working**

The level meter polls every 2 seconds which feels sluggish. Real-time feedback builds confidence the recording is working.

- [ ] Increase recording status poll to 500ms (or use Tauri events for push-based updates)
- [ ] Add CSS transition smoothing on the meter bar
- [ ] Add a peak hold indicator (thin line that decays slowly)
- [ ] Consider emitting levels via Tauri event from the stderr reader thread instead of polling

### 6. Elapsed time and file size display
**Job: Show it's working**

Show both elapsed time and current file size during recording so the user can gauge duration and disk usage at a glance.

- [ ] Add file size to `RecordingStatus` (stat the file in `get_recording_status`)
- [ ] Display formatted size next to elapsed time (e.g., "03:24 -- 19.2 MB")
- [ ] Warn if file exceeds a configurable threshold (e.g., 500 MB)

### 7. Pause / resume
**Job: Show it's working**

Allow pausing and resuming a recording without creating a new file. Useful for breaks in meetings or filtering out noise.

- [ ] Send SIGSTOP/SIGCONT to ffmpeg process for pause/resume
- [ ] Add Pause button that appears while recording is active
- [ ] Show "paused" state in UI (pulsing pause icon, frozen timer)
- [ ] Alternative: stop ffmpeg and re-open in append mode (may need post-concat)

### 8. Recordings list / file manager
**Job: Make output easy to find and use**

Add a panel or expandable section that lists past recordings with metadata.

- [ ] Add Tauri command `list_recordings` that scans the recordings directory
- [ ] Show list: filename, date, duration, size, device used
- [ ] Sort by date (newest first)
- [ ] Actions per recording: play, rename, delete, show in Finder, transcribe
- [ ] Paginate or virtual-scroll for large lists

### 9. In-app audio playback
**Job: Make output easy to find and use**

Play back recordings directly in the app without opening an external player.

- [ ] Use HTML5 `<audio>` element with a Tauri asset protocol URL or base64
- [ ] Add play/pause button to each recording in the list
- [ ] Show playback progress bar and current position
- [ ] Route playback to the selected output device

### 10. Rename and annotate recordings
**Job: Make output easy to find and use**

Auto-generated filenames like `recording_2026-03-18_190357.wav` are not searchable. Let users add a name or note.

- [ ] Add inline rename on the filename (click to edit)
- [ ] Store metadata in a sidecar `.json` file (name, notes, device, duration)
- [ ] Optional: auto-generate a title from the first few seconds of transcript

### 11. Auto-transcribe integration
**Job: Make output easy to find and use**

Leverage the existing audio-assist transcription pipeline to auto-transcribe completed recordings.

- [ ] After stop, offer a "Transcribe" button
- [ ] POST the WAV to the audio-assist `/v1/transcripts/postprocess` endpoint
- [ ] Show transcript inline in the recordings list
- [ ] Option to auto-transcribe on every recording stop

### 12. Keyboard shortcut for record toggle
**Job: Capture the right audio**

A global hotkey to start/stop recording without switching to the app window.

- [ ] Register a global shortcut (e.g., Cmd+Shift+R) via tauri-plugin-global-shortcut
- [ ] Toggle recording on press
- [ ] Show a brief system notification on start/stop

### 13. Max duration / disk space guard
**Job: Capture the right audio**

Prevent runaway recordings that fill the disk (the 1 GB orphan file from testing).

- [ ] Add configurable max duration (default: 4 hours)
- [ ] Add configurable max file size (default: 2 GB)
- [ ] Auto-stop with notification when limit is reached
- [ ] Check available disk space before starting

### 14. Graceful cleanup on app quit
**Job: Capture the right audio**

Ensure recordings are properly finalized when the app exits or crashes.

- [ ] Hook into Tauri's `before_exit` or `on_window_event(Destroyed)` to call `stop_recording`
- [ ] On app startup, detect and clean up orphaned ffmpeg processes
- [ ] Validate WAV headers of any in-progress files from prior sessions

### 15. Configurable output directory
**Job: Make output easy to find and use**

Let users choose where recordings are saved, with the default being `data/recordings/`.

- [ ] Add settings UI or config for output directory
- [ ] "Browse" button to pick a folder via Tauri's file dialog
- [ ] Persist choice across sessions
- [ ] Show current output path in the recording panel

---

## Suggested Priority Order

| Phase | Issues | Theme |
|-------|--------|-------|
| **P0 - Essentials** | 1, 4, 6, 13, 14 | Reliable, visible recording with device choice |
| **P1 - Daily driver** | 3, 5, 8, 12, 15 | Compressed formats, file management, shortcuts |
| **P2 - Power features** | 2, 7, 9, 10, 11 | Multi-source, playback, transcription |
