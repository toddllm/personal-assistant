use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;
#[cfg(target_os = "macos")]
use std::sync::mpsc;

pub struct HttpClient(pub reqwest::Client);

#[cfg(target_os = "macos")]
use block2::RcBlock;
#[cfg(target_os = "macos")]
use objc2_av_foundation::{
    AVAuthorizationStatus, AVCaptureDevice, AVMediaType, AVMediaTypeAudio,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ServiceDef {
    pub id: String,
    pub label: String,
    pub port: u16,
    #[serde(rename = "healthEndpoint")]
    pub health_endpoint: Option<String>,
    #[serde(rename = "uiUrl")]
    pub ui_url: String,
    pub script: Option<String>,
    pub entrypoint: Option<String>,
    #[serde(rename = "logFile")]
    pub log_file: String,
    pub autostart: bool,
    pub group: String,
}

#[derive(Debug, Serialize)]
pub struct HealthResult {
    pub service_id: String,
    pub status: String, // "green", "yellow", "red"
    pub detail: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct ServiceControlResult {
    pub service_id: String,
    pub action: String,
    pub success: bool,
    pub output: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct AudioRouteStatus {
    pub current_output: Option<String>,
    pub current_system_output: Option<String>,
    pub available_outputs: Vec<String>,
    pub recommended_output: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct MediaPermissionStatus {
    pub service: String,
    pub status: String,
    pub granted: bool,
    pub can_prompt: bool,
}

// --- Audio Recording ---

#[derive(Debug, Default)]
struct RecordingLevels {
    momentary_lufs: f64,
    peak_dbfs: f64,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct RecordingStatus {
    pub active: bool,
    pub file_path: Option<String>,
    pub input_device: Option<String>,
    pub started_at: Option<String>,
    pub level_dbfs: f64,
    pub peak_dbfs: f64,
    pub file_size_bytes: u64,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct AudioInputDevice {
    pub index: usize,
    pub name: String,
    pub is_default: bool,
}

// Max recording limits
const MAX_RECORDING_DURATION_SECS: u64 = 4 * 3600; // 4 hours
const MAX_RECORDING_FILE_BYTES: u64 = 2 * 1024 * 1024 * 1024; // 2 GB

struct RecordingTrack {
    child: std::process::Child,
    file_path: String,
    device_name: String,
}

pub struct RecorderState {
    tracks: Vec<RecordingTrack>,
    started_at: Option<String>,
    levels: Arc<Mutex<RecordingLevels>>,
}

impl RecorderState {
    pub fn new() -> Self {
        Self {
            tracks: Vec::new(),
            started_at: None,
            levels: Arc::new(Mutex::new(RecordingLevels::default())),
        }
    }

    fn total_file_size(&self) -> u64 {
        self.tracks
            .iter()
            .map(|t| std::fs::metadata(&t.file_path).map(|m| m.len()).unwrap_or(0))
            .sum()
    }

    fn primary_file_path(&self) -> Option<String> {
        self.tracks.first().map(|t| t.file_path.clone())
    }

    fn device_names(&self) -> Option<String> {
        if self.tracks.is_empty() {
            return None;
        }
        Some(
            self.tracks
                .iter()
                .map(|t| t.device_name.as_str())
                .collect::<Vec<_>>()
                .join(", "),
        )
    }

    fn status(&self) -> RecordingStatus {
        let lvl = self.levels.lock().unwrap();
        RecordingStatus {
            active: !self.tracks.is_empty(),
            file_path: self.primary_file_path(),
            input_device: self.device_names(),
            started_at: self.started_at.clone(),
            level_dbfs: lvl.momentary_lufs,
            peak_dbfs: lvl.peak_dbfs,
            file_size_bytes: self.total_file_size(),
        }
    }

    /// Stop all recording processes gracefully. Returns true if any were active.
    pub fn stop(&mut self) -> bool {
        if self.tracks.is_empty() {
            return false;
        }
        for track in &mut self.tracks {
            if let Some(ref mut stdin) = track.child.stdin {
                use std::io::Write;
                let _ = stdin.write_all(b"q");
            }
        }
        for track in &mut self.tracks {
            let _ = track.child.wait();
        }
        self.tracks.clear();
        self.started_at = None;
        self.levels = Arc::new(Mutex::new(RecordingLevels::default()));
        true
    }
}

pub struct Recorder(pub Mutex<RecorderState>);

fn monorepo_root() -> PathBuf {
    let manifest = env!("CARGO_MANIFEST_DIR");
    PathBuf::from(manifest)
        .parent()
        .and_then(|p| p.parent())
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("."))
}

fn run_command(program: &str, args: &[&str]) -> Result<String, String> {
    let out = Command::new(program)
        .args(args)
        .output()
        .map_err(|e| format!("failed to run {}: {}", program, e))?;
    if out.status.success() {
        Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
    } else {
        let stderr = String::from_utf8_lossy(&out.stderr).trim().to_string();
        let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
        let detail = if stderr.is_empty() { stdout } else { stderr };
        Err(if detail.is_empty() {
            format!("{} exited with {}", program, out.status)
        } else {
            detail
        })
    }
}

fn switch_audio_source(args: &[&str]) -> Result<String, String> {
    run_command("SwitchAudioSource", args)
}

fn current_audio_device(kind: &str) -> Result<Option<String>, String> {
    let value = switch_audio_source(&["-c", "-t", kind])?;
    if value.is_empty() {
        Ok(None)
    } else {
        Ok(Some(value))
    }
}

fn available_output_devices() -> Result<Vec<String>, String> {
    let text = switch_audio_source(&["-a", "-t", "output"])?;
    Ok(text
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .map(ToOwned::to_owned)
        .collect())
}

fn read_audio_route_status() -> Result<AudioRouteStatus, String> {
    Ok(AudioRouteStatus {
        current_output: current_audio_device("output")?,
        current_system_output: current_audio_device("system")?,
        available_outputs: available_output_devices()?,
        recommended_output: "CaptureAudio 2ch".to_string(),
    })
}

fn microphone_permission_status_for_state(status: &str, granted: bool, can_prompt: bool) -> MediaPermissionStatus {
    MediaPermissionStatus {
        service: "microphone".to_string(),
        status: status.to_string(),
        granted,
        can_prompt,
    }
}

#[cfg(target_os = "macos")]
fn microphone_media_type() -> Result<&'static AVMediaType, String> {
    // SAFETY: This is an immutable AVFoundation constant exported by the framework.
    unsafe {
        AVMediaTypeAudio
            .ok_or_else(|| "AVFoundation did not expose AVMediaTypeAudio.".to_string())
    }
}

#[cfg(target_os = "macos")]
fn map_microphone_permission_status(status: AVAuthorizationStatus) -> MediaPermissionStatus {
    if status == AVAuthorizationStatus::Authorized {
        return microphone_permission_status_for_state("authorized", true, false);
    }
    if status == AVAuthorizationStatus::Denied {
        return microphone_permission_status_for_state("denied", false, false);
    }
    if status == AVAuthorizationStatus::Restricted {
        return microphone_permission_status_for_state("restricted", false, false);
    }
    microphone_permission_status_for_state("not-determined", false, true)
}

#[cfg(target_os = "macos")]
fn read_microphone_permission_status() -> Result<MediaPermissionStatus, String> {
    let media_type = microphone_media_type()?;
    // SAFETY: AVFoundation requires a valid AVMediaType constant; AVMediaTypeAudio
    // is provided by the framework and is the documented key for microphone access.
    let status = unsafe { AVCaptureDevice::authorizationStatusForMediaType(media_type) };
    Ok(map_microphone_permission_status(status))
}

#[tauri::command]
pub async fn check_health(
    http: tauri::State<'_, HttpClient>,
    service_id: String,
    port: u16,
    health_endpoint: Option<String>,
) -> Result<HealthResult, String> {
    let client = http.0.clone();
    let endpoint = health_endpoint.unwrap_or_else(|| "/health".to_string());
    let url = format!("http://127.0.0.1:{}{}", port, endpoint);

    Ok(match client.get(&url).send().await {
        Ok(resp) if resp.status().is_success() => HealthResult {
            service_id,
            status: "green".to_string(),
            detail: Some("healthy".to_string()),
        },
        Ok(resp) => HealthResult {
            service_id,
            status: "yellow".to_string(),
            detail: Some(format!("HTTP {}", resp.status())),
        },
        Err(_) => {
            // No health endpoint — try a TCP connect to see if port is open
            let addr = format!("127.0.0.1:{}", port);
            match tokio::net::TcpStream::connect(&addr).await {
                Ok(_) => HealthResult {
                    service_id,
                    status: "yellow".to_string(),
                    detail: Some("port open, no health endpoint".to_string()),
                },
                Err(_) => HealthResult {
                    service_id,
                    status: "red".to_string(),
                    detail: Some("not reachable".to_string()),
                },
            }
        }
    })
}

#[tauri::command]
pub async fn service_control(
    service_id: String,
    script: Option<String>,
    entrypoint: Option<String>,
    action: String,
) -> ServiceControlResult {
    let root = monorepo_root();

    let (program, args): (String, Vec<String>) = if let Some(ref s) = script {
        let script_path = root.join(s);
        ("bash".to_string(), vec![
            script_path.to_string_lossy().to_string(),
            action.clone(),
        ])
    } else if let Some(ref e) = entrypoint {
        let bin_path = root.join(e);
        match action.as_str() {
            "start" => {
                // For entrypoint services, run in background
                ("bash".to_string(), vec![
                    "-c".to_string(),
                    format!(
                        "nohup {} > {} 2>&1 &",
                        bin_path.to_string_lossy(),
                        root.join("data/logs")
                            .join(format!("{}.log", service_id))
                            .to_string_lossy()
                    ),
                ])
            }
            "stop" => {
                ("bash".to_string(), vec![
                    "-c".to_string(),
                    format!(
                        "pkill -f {} 2>/dev/null || true",
                        bin_path.to_string_lossy()
                    ),
                ])
            }
            "restart" => {
                ("bash".to_string(), vec![
                    "-c".to_string(),
                    format!(
                        "pkill -f {} 2>/dev/null; sleep 1; nohup {} > {} 2>&1 &",
                        bin_path.to_string_lossy(),
                        bin_path.to_string_lossy(),
                        root.join("data/logs")
                            .join(format!("{}.log", service_id))
                            .to_string_lossy()
                    ),
                ])
            }
            _ => {
                return ServiceControlResult {
                    service_id,
                    action,
                    success: false,
                    output: "unknown action".to_string(),
                };
            }
        }
    } else {
        return ServiceControlResult {
            service_id,
            action,
            success: false,
            output: "no script or entrypoint configured".to_string(),
        };
    };

    match Command::new(&program).current_dir(&root).args(&args).output() {
        Ok(out) => {
            let stdout = String::from_utf8_lossy(&out.stdout).to_string();
            let stderr = String::from_utf8_lossy(&out.stderr).to_string();
            let combined = if stderr.is_empty() {
                stdout
            } else {
                format!("{}\n{}", stdout, stderr)
            };
            ServiceControlResult {
                service_id,
                action,
                success: out.status.success(),
                output: combined,
            }
        }
        Err(e) => ServiceControlResult {
            service_id,
            action,
            success: false,
            output: format!("failed to execute: {}", e),
        },
    }
}

#[tauri::command]
pub fn read_log_tail(log_file: String, lines: usize) -> Result<String, String> {
    use std::io::{Read, Seek, SeekFrom};

    let root = monorepo_root();
    let path = root.join(&log_file);

    if !path.exists() {
        return Ok(format!("(no log file: {})", log_file));
    }

    let mut file = std::fs::File::open(&path).map_err(|e| e.to_string())?;
    let file_len = file.metadata().map_err(|e| e.to_string())?.len();

    if file_len == 0 {
        return Ok(String::new());
    }

    // Read at most 64 KB from the end — plenty for 80 lines
    let read_size: u64 = 65_536;
    let start_pos = file_len.saturating_sub(read_size);
    file.seek(SeekFrom::Start(start_pos)).map_err(|e| e.to_string())?;

    let mut buf = String::new();
    file.read_to_string(&mut buf).map_err(|e| e.to_string())?;

    // If we started mid-file, drop the first partial line
    if start_pos > 0 {
        if let Some(pos) = buf.find('\n') {
            buf = buf[pos + 1..].to_string();
        }
    }

    // Take last N lines
    let all_lines: Vec<&str> = buf.lines().collect();
    let start = all_lines.len().saturating_sub(lines);
    Ok(all_lines[start..].join("\n"))
}

#[tauri::command]
pub fn get_services() -> Result<Vec<ServiceDef>, String> {
    let root = monorepo_root();
    let path = root.join("app/services.json");
    let data = std::fs::read_to_string(&path)
        .map_err(|e| format!("failed to read services.json: {}", e))?;
    serde_json::from_str(&data).map_err(|e| format!("invalid services.json: {}", e))
}

#[tauri::command]
pub fn get_monorepo_root() -> String {
    monorepo_root().to_string_lossy().to_string()
}

#[tauri::command]
pub async fn fetch_local_api(
    http: tauri::State<'_, HttpClient>,
    url: String,
    method: Option<String>,
    body: Option<String>,
) -> Result<String, String> {
    // Only allow localhost requests
    if !url.starts_with("http://127.0.0.1:") && !url.starts_with("http://localhost:") {
        return Err("only localhost URLs allowed".to_string());
    }

    let client = http.0.clone();
    let method_str = method.unwrap_or_else(|| "GET".to_string());
    let req = match method_str.to_uppercase().as_str() {
        "POST" => {
            let mut r = client.post(&url);
            if let Some(b) = body {
                r = r.header("Content-Type", "application/json").body(b);
            }
            r
        }
        "DELETE" => client.delete(&url),
        _ => client.get(&url),
    };

    let resp = req.send().await.map_err(|e| e.to_string())?;
    resp.text().await.map_err(|e| e.to_string())
}

#[tauri::command]
pub fn get_audio_route_status() -> Result<AudioRouteStatus, String> {
    read_audio_route_status()
}

#[tauri::command]
pub fn restore_capture_audio_defaults() -> Result<AudioRouteStatus, String> {
    let target = "CaptureAudio 2ch";
    switch_audio_source(&["-s", target, "-t", "output"])?;
    switch_audio_source(&["-s", target, "-t", "system"])?;
    read_audio_route_status()
}

#[tauri::command]
pub fn get_microphone_permission_status() -> Result<MediaPermissionStatus, String> {
    #[cfg(target_os = "macos")]
    {
        return read_microphone_permission_status();
    }

    #[cfg(not(target_os = "macos"))]
    {
        Ok(microphone_permission_status_for_state("unsupported", false, false))
    }
}

#[tauri::command]
pub fn request_microphone_permission() -> Result<MediaPermissionStatus, String> {
    #[cfg(target_os = "macos")]
    {
        let current = read_microphone_permission_status()?;
        if current.granted || !current.can_prompt {
            return Ok(current);
        }

        let media_type = microphone_media_type()?;
        let (tx, rx) = mpsc::sync_channel(1);
        let handler = RcBlock::new(move |granted| {
            let _ = tx.send(bool::from(granted));
        });

        // SAFETY: The callback is retained by AVFoundation for the lifetime of the
        // async permission request, and the media type constant is valid.
        unsafe {
            AVCaptureDevice::requestAccessForMediaType_completionHandler(media_type, &handler);
        }

        let granted = rx
            .recv_timeout(Duration::from_secs(30))
            .map_err(|_| "Timed out waiting for the macOS microphone permission prompt.".to_string())?;
        if granted {
            return Ok(microphone_permission_status_for_state("authorized", true, false));
        }
        read_microphone_permission_status()
    }

    #[cfg(not(target_os = "macos"))]
    {
        Ok(microphone_permission_status_for_state("unsupported", false, false))
    }
}

// --- Audio Recording Commands ---

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
pub struct RecordingInfo {
    pub file_name: String,
    pub file_path: String,
    pub size_bytes: u64,
    pub created_at: String, // ISO 8601
    pub duration_seconds: Option<f64>,
}

fn recorder_config_path() -> PathBuf {
    monorepo_root().join("data").join("recorder-config.json")
}

fn default_recordings_dir() -> PathBuf {
    // Read from config file first, fall back to data/recordings/
    if let Ok(data) = std::fs::read_to_string(recorder_config_path()) {
        if let Ok(cfg) = serde_json::from_str::<serde_json::Value>(&data) {
            if let Some(dir) = cfg.get("output_dir").and_then(|v| v.as_str()) {
                let p = PathBuf::from(dir);
                if !dir.is_empty() {
                    return p;
                }
            }
        }
    }
    monorepo_root().join("data").join("recordings")
}

#[tauri::command]
pub fn list_audio_input_devices() -> Result<Vec<AudioInputDevice>, String> {
    let output = Command::new("ffmpeg")
        .args(["-f", "avfoundation", "-list_devices", "true", "-i", ""])
        .output()
        .map_err(|e| format!("failed to run ffmpeg: {}", e))?;

    // ffmpeg prints device list to stderr
    let stderr = String::from_utf8_lossy(&output.stderr);
    let default_input = current_audio_device("input").ok().flatten();

    let mut devices = Vec::new();
    let mut in_audio = false;
    // Lines: "[AVFoundation indev @ 0x...] [0] CaptureAudio 2ch"
    let re = regex::Regex::new(r"\] \[(\d+)\] (.+)$").unwrap();
    for line in stderr.lines() {
        if line.contains("AVFoundation audio devices:") {
            in_audio = true;
            continue;
        }
        if !in_audio {
            continue;
        }
        // Stop if we hit an error line (end of device list)
        if line.contains("Error") || (!line.contains('[') && !line.trim().is_empty()) {
            break;
        }
        if let Some(caps) = re.captures(line) {
            let index: usize = caps[1].parse().unwrap_or(0);
            let name = caps[2].trim().to_string();
            let is_default = default_input.as_deref() == Some(&name);
            devices.push(AudioInputDevice { index, name, is_default });
        }
    }
    Ok(devices)
}

#[tauri::command]
pub fn get_recording_status(recorder: tauri::State<'_, Recorder>) -> RecordingStatus {
    let mut state = recorder.0.lock().unwrap();

    // Auto-stop if limits exceeded
    if !state.tracks.is_empty() {
        let should_stop = {
            let size = state.total_file_size();
            let duration_exceeded = state.started_at.as_ref().map_or(false, |s| {
                chrono::DateTime::parse_from_rfc3339(s)
                    .map(|start| {
                        let elapsed = chrono::Local::now().signed_duration_since(start);
                        elapsed.num_seconds() as u64 > MAX_RECORDING_DURATION_SECS
                    })
                    .unwrap_or(false)
            });
            size > MAX_RECORDING_FILE_BYTES || duration_exceeded
        };
        if should_stop {
            state.stop();
        }
    }

    state.status()
}

fn kill_stale_recording_ffmpeg() {
    // Kill any orphaned ffmpeg processes from prior app sessions
    let _ = Command::new("pkill")
        .args(["-f", "ffmpeg.*avfoundation.*data/recordings"])
        .output();
}

/// Stop any active recording. Called on app exit.
pub fn stop_active_recording(recorder: &Recorder) {
    if let Ok(mut state) = recorder.0.lock() {
        state.stop();
    }
}

fn resolve_device_name(device: &str) -> String {
    if device == "default" {
        // Try to resolve the actual default input device name
        current_audio_device("input")
            .ok()
            .flatten()
            .unwrap_or_else(|| device.to_string())
    } else {
        device.to_string()
    }
}

fn spawn_level_reader(stderr: std::process::ChildStderr, levels: Arc<Mutex<RecordingLevels>>) {
    std::thread::spawn(move || {
        use std::io::{BufRead, BufReader};
        let reader = BufReader::new(stderr);
        for line in reader.lines() {
            let line = match line {
                Ok(l) => l,
                Err(_) => break,
            };
            // Parse ebur128 output: "... M: -20.3 S: ... FTPK: -10.2 -10.2 dBFS ..."
            if !line.contains("Parsed_ebur128") {
                continue;
            }
            let mut m_val = None;
            let mut p_val = None;

            if let Some(pos) = line.find("M:") {
                let after = &line[pos + 2..];
                let token = after.trim_start().split_whitespace().next().unwrap_or("");
                m_val = token.parse::<f64>().ok();
            }
            if let Some(pos) = line.find("FTPK:") {
                let after = &line[pos + 5..];
                let token = after.trim_start().split_whitespace().next().unwrap_or("");
                p_val = token.parse::<f64>().ok();
            }

            if m_val.is_some() || p_val.is_some() {
                if let Ok(mut lvl) = levels.lock() {
                    if let Some(m) = m_val {
                        lvl.momentary_lufs = m;
                    }
                    if let Some(p) = p_val {
                        lvl.peak_dbfs = p;
                    }
                }
            }
        }
    });
}

#[tauri::command]
pub fn start_recording(
    recorder: tauri::State<'_, Recorder>,
    input_devices: Option<Vec<String>>,
    output_dir: Option<String>,
    format: Option<String>,
) -> Result<RecordingStatus, String> {
    let mut state = recorder.0.lock().unwrap();
    if !state.tracks.is_empty() {
        return Err("recording already in progress".to_string());
    }

    // Clean up any orphaned ffmpeg recording processes from prior app sessions
    kill_stale_recording_ffmpeg();

    let dir = output_dir
        .map(PathBuf::from)
        .unwrap_or_else(default_recordings_dir);
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("failed to create recordings directory: {}", e))?;

    let devices = input_devices
        .filter(|d| !d.is_empty())
        .unwrap_or_else(|| vec!["default".to_string()]);

    let now = chrono::Local::now();
    let timestamp = now.format("%Y-%m-%d_%H%M%S");
    let levels = Arc::new(Mutex::new(RecordingLevels::default()));
    let mut first_error: Option<String> = None;
    let fmt = format.unwrap_or_else(|| "wav".to_string()).to_lowercase();
    let file_ext = match fmt.as_str() {
        "flac" => "flac",
        "mp3" => "mp3",
        _ => "wav",
    };

    for (i, device_arg) in devices.iter().enumerate() {
        let device_display = resolve_device_name(device_arg);
        let suffix = if devices.len() > 1 {
            // Sanitize device name for filename
            let safe: String = device_display
                .chars()
                .map(|c| if c.is_alphanumeric() || c == '-' { c } else { '_' })
                .collect();
            format!("__{}", safe)
        } else {
            String::new()
        };
        let filename = format!("recording_{}{}.{}", timestamp, suffix, file_ext);
        let file_path = dir.join(&filename);

        // Only attach ebur128 level reader to the first track
        let use_ebur128 = i == 0;
        let af_arg = if use_ebur128 { "ebur128=peak=true" } else { "anull" };

        // Build codec args based on format
        let mut ffmpeg_args: Vec<String> = vec![
            "-f".to_string(), "avfoundation".to_string(),
            "-i".to_string(), format!(":{}", device_arg),
            "-af".to_string(), af_arg.to_string(),
            "-ac".to_string(), "1".to_string(),
            "-ar".to_string(), "48000".to_string(),
        ];
        match file_ext {
            "flac" => {
                ffmpeg_args.extend(["-c:a".to_string(), "flac".to_string()]);
            }
            "mp3" => {
                ffmpeg_args.extend([
                    "-c:a".to_string(), "libmp3lame".to_string(),
                    "-q:a".to_string(), "2".to_string(),
                ]);
            }
            _ => {
                // wav: keep sample format
                ffmpeg_args.extend(["-sample_fmt".to_string(), "s16".to_string()]);
            }
        }
        ffmpeg_args.extend(["-y".to_string(), file_path.to_str().unwrap().to_string()]);

        let ffmpeg_arg_refs: Vec<&str> = ffmpeg_args.iter().map(|s| s.as_str()).collect();

        let mut child = match Command::new("ffmpeg")
            .args(&ffmpeg_arg_refs)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::piped())
            .spawn()
        {
            Ok(c) => c,
            Err(e) => {
                if first_error.is_none() {
                    first_error = Some(format!("failed to start ffmpeg for {}: {}", device_display, e));
                }
                continue;
            }
        };

        if use_ebur128 {
            if let Some(stderr) = child.stderr.take() {
                spawn_level_reader(stderr, Arc::clone(&levels));
            }
        }

        state.tracks.push(RecordingTrack {
            child,
            file_path: file_path.to_string_lossy().to_string(),
            device_name: device_display,
        });
    }

    if state.tracks.is_empty() {
        return Err(first_error.unwrap_or_else(|| "no devices to record from".to_string()));
    }

    state.started_at = Some(now.to_rfc3339());
    state.levels = levels;

    Ok(state.status())
}

#[tauri::command]
pub fn stop_recording(recorder: tauri::State<'_, Recorder>) -> Result<RecordingStatus, String> {
    let mut state = recorder.0.lock().unwrap();
    // Capture info before stopping
    let file_path = state.primary_file_path();
    let device_names = state.device_names();
    let file_size = state.total_file_size();

    if !state.stop() {
        return Err("no recording in progress".to_string());
    }

    Ok(RecordingStatus {
        active: false,
        file_path,
        input_device: device_names,
        started_at: None,
        level_dbfs: 0.0,
        peak_dbfs: 0.0,
        file_size_bytes: file_size,
    })
}

#[tauri::command]
pub fn get_recording_output_dir() -> String {
    default_recordings_dir().to_string_lossy().to_string()
}

#[tauri::command]
pub fn set_recording_output_dir(path: String) -> Result<String, String> {
    let config_path = recorder_config_path();
    // Ensure parent dir exists
    if let Some(parent) = config_path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| format!("failed to create config directory: {}", e))?;
    }
    let cfg = serde_json::json!({ "output_dir": path });
    std::fs::write(&config_path, serde_json::to_string_pretty(&cfg).unwrap())
        .map_err(|e| format!("failed to write config: {}", e))?;
    Ok(path)
}

#[tauri::command]
pub fn reveal_recording_output_dir() -> Result<(), String> {
    let dir = default_recordings_dir();
    std::fs::create_dir_all(&dir)
        .map_err(|e| format!("failed to create directory: {}", e))?;
    Command::new("open")
        .arg(dir.to_str().unwrap_or("."))
        .spawn()
        .map_err(|e| format!("failed to open Finder: {}", e))?;
    Ok(())
}

#[tauri::command]
pub fn list_recordings() -> Result<Vec<RecordingInfo>, String> {
    let dir = default_recordings_dir();
    if !dir.exists() {
        return Ok(Vec::new());
    }

    let entries =
        std::fs::read_dir(&dir).map_err(|e| format!("failed to read recordings dir: {}", e))?;
    let extensions = ["wav", "flac", "mp3"];
    let mut recordings = Vec::new();

    for entry in entries {
        let entry = match entry {
            Ok(e) => e,
            Err(_) => continue,
        };
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let ext = path
            .extension()
            .and_then(|e| e.to_str())
            .unwrap_or("")
            .to_lowercase();
        if !extensions.contains(&ext.as_str()) {
            continue;
        }

        let metadata = match std::fs::metadata(&path) {
            Ok(m) => m,
            Err(_) => continue,
        };

        let file_name = path
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or("")
            .to_string();
        let file_path = path.to_string_lossy().to_string();
        let size_bytes = metadata.len();

        let created_at = metadata
            .created()
            .or_else(|_| metadata.modified())
            .map(|t| {
                let dt: chrono::DateTime<chrono::Local> = t.into();
                dt.to_rfc3339()
            })
            .unwrap_or_default();

        // Estimate duration for WAV files: size / (sample_rate * bytes_per_sample * channels)
        // 48000 Hz, 16-bit (2 bytes), mono (1 channel) = 96000 bytes/sec
        // Subtract 44-byte WAV header
        let duration_seconds = if ext == "wav" {
            let data_bytes = size_bytes.saturating_sub(44);
            Some(data_bytes as f64 / (48000.0 * 2.0 * 1.0))
        } else {
            None
        };

        recordings.push(RecordingInfo {
            file_name,
            file_path,
            size_bytes,
            created_at,
            duration_seconds,
        });
    }

    // Sort by created date, newest first
    recordings.sort_by(|a, b| b.created_at.cmp(&a.created_at));

    Ok(recordings)
}

#[tauri::command]
pub fn delete_recording(file_path: String) -> Result<(), String> {
    let recordings_dir = default_recordings_dir();
    let target = PathBuf::from(&file_path);

    // Security check: ensure path is within recordings directory
    let canonical_dir = recordings_dir
        .canonicalize()
        .map_err(|e| format!("recordings dir not found: {}", e))?;
    let canonical_target = target
        .canonicalize()
        .map_err(|e| format!("file not found: {}", e))?;

    if !canonical_target.starts_with(&canonical_dir) {
        return Err("path is outside the recordings directory".to_string());
    }

    std::fs::remove_file(&canonical_target)
        .map_err(|e| format!("failed to delete recording: {}", e))
}
