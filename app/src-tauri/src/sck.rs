//! ScreenCaptureKit integration for per-app audio capture (macOS only).
//!
//! Phase 1: Permission check + list capturable apps
//! Phase 2: Start/stop SCK audio streams, write to WAV via hound

use serde::Serialize;

// ─── Data types ──────────────────────────────────────────────────────────────

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct ScreenRecordingPermissionStatus {
    pub available: bool,
    pub granted: bool,
    pub error: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct CapturableApp {
    pub bundle_id: String,
    pub name: String,
    pub pid: i32,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct SckRecordingStatus {
    pub active: bool,
    pub paused: bool,
    pub file_path: Option<String>,
    pub app_names: Vec<String>,
    pub started_at: Option<String>,
    pub level_dbfs: f64,
    pub peak_dbfs: f64,
    pub file_size_bytes: u64,
}

/// Bundle IDs of well-known audio applications, sorted to the top of listings.
const AUDIO_APP_BUNDLE_IDS: &[&str] = &[
    "com.google.Chrome",
    "com.apple.Safari",
    "us.zoom.xos",
    "com.spotify.client",
    "com.apple.Music",
    "com.hnc.Discord",
    "com.tinyspeck.slackmacgap",
    "company.thebrowser.Browser", // Arc
    "org.mozilla.firefox",
    "com.microsoft.teams2",
];

// ─── macOS implementation ────────────────────────────────────────────────────

#[cfg(target_os = "macos")]
mod platform {
    use super::*;
    use crate::commands::RecordingLevels;
    use screencapturekit::prelude::*;
    use std::path::PathBuf;
    use std::sync::{Arc, Mutex, mpsc};

    /// Internal state for the SCK recorder.
    pub struct SckRecorderState {
        stream: Option<SCStream>,
        writer_handle: Option<std::thread::JoinHandle<()>>,
        stop_tx: Option<mpsc::Sender<()>>,
        file_path: Option<String>,
        started_at: Option<String>,
        app_names: Vec<String>,
        levels: Arc<Mutex<RecordingLevels>>,
        paused: bool,
    }

    // SAFETY: SCStream is Send+Sync per the crate. All other fields are safe.
    unsafe impl Send for SckRecorderState {}

    impl SckRecorderState {
        pub fn new() -> Self {
            Self {
                stream: None,
                writer_handle: None,
                stop_tx: None,
                file_path: None,
                started_at: None,
                app_names: Vec::new(),
                levels: Arc::new(Mutex::new(RecordingLevels::default())),
                paused: false,
            }
        }

        pub fn status(&self) -> SckRecordingStatus {
            let lvl = self.levels.lock().unwrap();
            let file_size = self
                .file_path
                .as_ref()
                .and_then(|p| std::fs::metadata(p).ok())
                .map(|m| m.len())
                .unwrap_or(0);
            SckRecordingStatus {
                active: self.stream.is_some(),
                paused: self.paused,
                file_path: self.file_path.clone(),
                app_names: self.app_names.clone(),
                started_at: self.started_at.clone(),
                level_dbfs: lvl.momentary_lufs,
                peak_dbfs: lvl.peak_dbfs,
                file_size_bytes: file_size,
            }
        }

        /// Stop the stream and writer thread. Returns true if a stream was active.
        pub fn stop(&mut self) -> bool {
            let had_stream = self.stream.is_some();
            if let Some(ref stream) = self.stream {
                let _ = stream.stop_capture();
            }
            self.stream = None;

            // Signal writer thread to stop
            if let Some(tx) = self.stop_tx.take() {
                let _ = tx.send(());
            }

            // Wait for writer thread to finish
            if let Some(handle) = self.writer_handle.take() {
                let _ = handle.join();
            }

            self.started_at = None;
            self.app_names.clear();
            self.levels = Arc::new(Mutex::new(RecordingLevels::default()));
            self.paused = false;
            had_stream
        }
    }

    // ── Permission check ─────────────────────────────────────────────────

    pub fn get_screen_recording_permission_status() -> ScreenRecordingPermissionStatus {
        match SCShareableContent::get() {
            Ok(content) => {
                let apps = content.applications();
                ScreenRecordingPermissionStatus {
                    available: true,
                    granted: !apps.is_empty(),
                    error: None,
                }
            }
            Err(e) => ScreenRecordingPermissionStatus {
                available: true,
                granted: false,
                error: Some(format!("{}", e)),
            },
        }
    }

    pub fn open_screen_recording_settings() -> Result<(), String> {
        std::process::Command::new("open")
            .arg("x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")
            .spawn()
            .map_err(|e| format!("failed to open System Preferences: {}", e))?;
        Ok(())
    }

    // ── List capturable apps ─────────────────────────────────────────────

    pub fn list_capturable_apps() -> Result<Vec<CapturableApp>, String> {
        let content = SCShareableContent::get().map_err(|e| format!("{}", e))?;
        let apps = content.applications();

        let mut result: Vec<CapturableApp> = apps
            .iter()
            .map(|app| CapturableApp {
                bundle_id: app.bundle_identifier(),
                name: app.application_name(),
                pid: app.process_id(),
            })
            .filter(|a| !a.name.is_empty())
            .collect();

        // Sort: known audio apps first, then alphabetically
        result.sort_by(|a, b| {
            let a_priority = AUDIO_APP_BUNDLE_IDS
                .iter()
                .position(|id| *id == a.bundle_id);
            let b_priority = AUDIO_APP_BUNDLE_IDS
                .iter()
                .position(|id| *id == b.bundle_id);
            match (a_priority, b_priority) {
                (Some(ai), Some(bi)) => ai.cmp(&bi),
                (Some(_), None) => std::cmp::Ordering::Less,
                (None, Some(_)) => std::cmp::Ordering::Greater,
                (None, None) => a.name.to_lowercase().cmp(&b.name.to_lowercase()),
            }
        });

        Ok(result)
    }

    // ── Start/stop SCK audio recording ───────────────────────────────────

    pub fn start_recording(
        state: &mut SckRecorderState,
        app_bundle_ids: Vec<String>,
        include_mic: bool,
        output_dir: Option<String>,
        _format: Option<String>, // reserved; we always write WAV for now
    ) -> Result<SckRecordingStatus, String> {
        if state.stream.is_some() {
            return Err("SCK recording already in progress".to_string());
        }

        // Resolve output directory
        let dir = output_dir
            .map(PathBuf::from)
            .unwrap_or_else(|| {
                let manifest = env!("CARGO_MANIFEST_DIR");
                PathBuf::from(manifest)
                    .parent()
                    .and_then(|p| p.parent())
                    .map(|p| p.to_path_buf())
                    .unwrap_or_else(|| PathBuf::from("."))
                    .join("data")
                    .join("recordings")
            });
        std::fs::create_dir_all(&dir)
            .map_err(|e| format!("failed to create recordings dir: {}", e))?;

        // Get shareable content
        let content = SCShareableContent::get().map_err(|e| format!("{}", e))?;
        let all_apps = content.applications();
        let display = content
            .displays()
            .into_iter()
            .next()
            .ok_or_else(|| "no display found".to_string())?;

        // Find the requested apps
        let matching_apps: Vec<&SCRunningApplication> = all_apps
            .iter()
            .filter(|a| app_bundle_ids.contains(&a.bundle_identifier()))
            .collect();

        if matching_apps.is_empty() {
            return Err(format!(
                "none of the requested apps are running: {:?}",
                app_bundle_ids
            ));
        }

        let app_names: Vec<String> = matching_apps
            .iter()
            .map(|a| a.application_name())
            .collect();

        // Build content filter — include only the requested apps
        let app_refs: Vec<&SCRunningApplication> = matching_apps.iter().copied().collect();
        let filter = SCContentFilter::create()
            .with_display(&display)
            .with_including_applications(&app_refs, &[])
            .build();

        // Configure: audio-only (minimal video to keep SCK happy)
        let mut config = SCStreamConfiguration::new()
            .with_width(2)
            .with_height(2)
            .with_captures_audio(true)
            .with_sample_rate(48000)
            .with_channel_count(1)
            .with_excludes_current_process_audio(true);

        if include_mic {
            config = config.with_captures_microphone(true);
        }

        // WAV file path
        let now = chrono::Local::now();
        let timestamp = now.format("%Y-%m-%d_%H%M%S");
        let safe_names: String = app_names
            .iter()
            .map(|n| {
                n.chars()
                    .map(|c| if c.is_alphanumeric() || c == '-' { c } else { '_' })
                    .collect::<String>()
            })
            .collect::<Vec<_>>()
            .join("_");
        let filename = format!("sck_{}_{}.wav", timestamp, safe_names);
        let file_path = dir.join(&filename);
        let file_path_str = file_path.to_string_lossy().to_string();

        // Channel for audio data: sender in the SCK callback, receiver in writer thread
        let (audio_tx, audio_rx) = mpsc::channel::<Vec<u8>>();
        let (stop_tx, stop_rx) = mpsc::channel::<()>();

        // Levels shared between callback and state
        let levels = Arc::new(Mutex::new(RecordingLevels::default()));
        let levels_for_callback = Arc::clone(&levels);

        // Start writer thread
        let writer_path = file_path_str.clone();
        let writer_handle = std::thread::spawn(move || {
            let spec = hound::WavSpec {
                channels: 1,
                sample_rate: 48000,
                bits_per_sample: 32,
                sample_format: hound::SampleFormat::Float,
            };
            let writer = match hound::WavWriter::create(&writer_path, spec) {
                Ok(w) => w,
                Err(e) => {
                    eprintln!("sck: failed to create WAV writer: {}", e);
                    return;
                }
            };
            let writer = Arc::new(Mutex::new(writer));

            loop {
                // Check for stop signal (non-blocking)
                if stop_rx.try_recv().is_ok() {
                    break;
                }
                // Receive audio data with a timeout so we can check stop_rx
                match audio_rx.recv_timeout(std::time::Duration::from_millis(100)) {
                    Ok(data) => {
                        // Data is f32 samples (little-endian) from SCK
                        let mut w = writer.lock().unwrap();
                        // Write f32 samples
                        let float_samples: &[f32] = unsafe {
                            std::slice::from_raw_parts(
                                data.as_ptr() as *const f32,
                                data.len() / 4,
                            )
                        };
                        for &sample in float_samples {
                            if w.write_sample(sample).is_err() {
                                break;
                            }
                        }
                    }
                    Err(mpsc::RecvTimeoutError::Timeout) => continue,
                    Err(mpsc::RecvTimeoutError::Disconnected) => break,
                }
            }

            // Finalize WAV
            if let Ok(w) = Arc::try_unwrap(writer) {
                let _ = w.into_inner().unwrap().finalize();
            }
        });

        // Create SCStream
        let mut stream = SCStream::new(&filter, &config);

        // Add audio output handler
        let tx = audio_tx.clone();
        let levels_cb = levels_for_callback;
        stream.add_output_handler(
            move |sample: CMSampleBuffer, of_type: SCStreamOutputType| {
                if of_type != SCStreamOutputType::Audio {
                    return;
                }
                if let Some(buf_list) = sample.audio_buffer_list() {
                    for buf in buf_list.iter() {
                        let data = buf.data();
                        if data.is_empty() {
                            continue;
                        }

                        // Compute peak level from f32 samples
                        let float_samples: &[f32] = unsafe {
                            std::slice::from_raw_parts(
                                data.as_ptr() as *const f32,
                                data.len() / 4,
                            )
                        };
                        let peak = float_samples
                            .iter()
                            .map(|s| s.abs())
                            .fold(0.0_f32, f32::max);
                        let peak_db = if peak > 0.0 {
                            20.0 * (peak as f64).log10()
                        } else {
                            -100.0
                        };

                        if let Ok(mut lvl) = levels_cb.lock() {
                            lvl.peak_dbfs = peak_db;
                            lvl.momentary_lufs = peak_db; // simplified
                        }

                        // Send to writer
                        let _ = tx.send(data.to_vec());
                    }
                }
            },
            SCStreamOutputType::Audio,
        );

        // Start capture
        stream
            .start_capture()
            .map_err(|e| format!("failed to start SCK capture: {}", e))?;

        state.stream = Some(stream);
        state.writer_handle = Some(writer_handle);
        state.stop_tx = Some(stop_tx);
        state.file_path = Some(file_path_str);
        state.started_at = Some(now.to_rfc3339());
        state.app_names = app_names;
        state.levels = levels;
        state.paused = false;

        Ok(state.status())
    }

    pub fn stop_recording(state: &mut SckRecorderState) -> Result<SckRecordingStatus, String> {
        let status_before = state.status();
        if !state.stop() {
            return Err("no SCK recording in progress".to_string());
        }
        Ok(SckRecordingStatus {
            active: false,
            paused: false,
            file_path: status_before.file_path,
            app_names: status_before.app_names,
            started_at: None,
            level_dbfs: 0.0,
            peak_dbfs: 0.0,
            file_size_bytes: status_before.file_size_bytes,
        })
    }

    pub fn get_recording_status(state: &SckRecorderState) -> SckRecordingStatus {
        state.status()
    }
}

// ─── Non-macOS stubs ─────────────────────────────────────────────────────────

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::*;

    pub struct SckRecorderState;

    impl SckRecorderState {
        pub fn new() -> Self {
            Self
        }
    }

    pub fn get_screen_recording_permission_status() -> ScreenRecordingPermissionStatus {
        ScreenRecordingPermissionStatus {
            available: false,
            granted: false,
            error: Some("ScreenCaptureKit is only available on macOS".to_string()),
        }
    }

    pub fn open_screen_recording_settings() -> Result<(), String> {
        Err("ScreenCaptureKit is only available on macOS".to_string())
    }

    pub fn list_capturable_apps() -> Result<Vec<CapturableApp>, String> {
        Err("ScreenCaptureKit is only available on macOS".to_string())
    }

    pub fn start_recording(
        _state: &mut SckRecorderState,
        _app_bundle_ids: Vec<String>,
        _include_mic: bool,
        _output_dir: Option<String>,
        _format: Option<String>,
    ) -> Result<SckRecordingStatus, String> {
        Err("ScreenCaptureKit is only available on macOS".to_string())
    }

    pub fn stop_recording(
        _state: &mut SckRecorderState,
    ) -> Result<SckRecordingStatus, String> {
        Err("ScreenCaptureKit is only available on macOS".to_string())
    }

    pub fn get_recording_status(_state: &SckRecorderState) -> SckRecordingStatus {
        SckRecordingStatus {
            active: false,
            paused: false,
            file_path: None,
            app_names: vec![],
            started_at: None,
            level_dbfs: 0.0,
            peak_dbfs: 0.0,
            file_size_bytes: 0,
        }
    }
}

// ─── Re-exports ──────────────────────────────────────────────────────────────

pub use platform::SckRecorderState;

/// Managed state wrapper for Tauri.
pub struct SckRecorder(pub std::sync::Mutex<SckRecorderState>);

/// Stop any active SCK recording (called on app exit).
pub fn stop_active_sck_recording(recorder: &SckRecorder) {
    if let Ok(mut state) = recorder.0.lock() {
        platform::stop_recording(&mut state).ok();
    }
}

// ─── Tauri Commands ──────────────────────────────────────────────────────────

#[tauri::command]
pub fn get_screen_recording_permission_status() -> ScreenRecordingPermissionStatus {
    platform::get_screen_recording_permission_status()
}

#[tauri::command]
pub fn open_screen_recording_settings() -> Result<(), String> {
    platform::open_screen_recording_settings()
}

#[tauri::command]
pub fn list_capturable_apps() -> Result<Vec<CapturableApp>, String> {
    platform::list_capturable_apps()
}

#[tauri::command]
pub fn start_sck_recording(
    recorder: tauri::State<'_, SckRecorder>,
    app_bundle_ids: Vec<String>,
    include_mic: Option<bool>,
    output_dir: Option<String>,
    format: Option<String>,
) -> Result<SckRecordingStatus, String> {
    let mut state = recorder.0.lock().unwrap();
    platform::start_recording(
        &mut state,
        app_bundle_ids,
        include_mic.unwrap_or(false),
        output_dir,
        format,
    )
}

#[tauri::command]
pub fn stop_sck_recording(
    recorder: tauri::State<'_, SckRecorder>,
) -> Result<SckRecordingStatus, String> {
    let mut state = recorder.0.lock().unwrap();
    platform::stop_recording(&mut state)
}

#[tauri::command]
pub fn get_sck_recording_status(
    recorder: tauri::State<'_, SckRecorder>,
) -> SckRecordingStatus {
    let state = recorder.0.lock().unwrap();
    platform::get_recording_status(&state)
}
