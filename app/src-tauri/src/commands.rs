use serde::{Deserialize, Serialize};
use std::io::{BufRead, BufReader};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;
#[cfg(target_os = "macos")]
use std::sync::mpsc;

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
    service_id: String,
    port: u16,
    health_endpoint: Option<String>,
) -> HealthResult {
    let endpoint = health_endpoint.unwrap_or_else(|| "/health".to_string());
    let url = format!("http://127.0.0.1:{}{}", port, endpoint);

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .build()
        .unwrap_or_default();

    match client.get(&url).send().await {
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
    }
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
    let root = monorepo_root();
    let path = root.join(&log_file);

    if !path.exists() {
        return Ok(format!("(no log file: {})", log_file));
    }

    let file = std::fs::File::open(&path).map_err(|e| e.to_string())?;
    let reader = BufReader::new(file);
    let all_lines: Vec<String> = reader.lines().filter_map(|l| l.ok()).collect();
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
    url: String,
    method: Option<String>,
    body: Option<String>,
) -> Result<String, String> {
    // Only allow localhost requests
    if !url.starts_with("http://127.0.0.1:") && !url.starts_with("http://localhost:") {
        return Err("only localhost URLs allowed".to_string());
    }

    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .map_err(|e| e.to_string())?;

    let method_str = method.unwrap_or_else(|| "GET".to_string());
    let req = match method_str.to_uppercase().as_str() {
        "POST" => {
            let mut r = client.post(&url);
            if let Some(b) = body {
                r = r.header("Content-Type", "application/json").body(b);
            }
            r
        }
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
