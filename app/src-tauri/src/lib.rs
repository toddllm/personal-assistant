mod commands;
mod sck;

use tauri::{
    Emitter,
    include_image,
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Manager,
};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

const WINDOW_VISIBILITY_EVENT: &str = "personal-assistant://window-visibility";

fn hide_window(window: &tauri::Window) {
    let _ = window.emit(WINDOW_VISIBILITY_EVENT, false);
    let _ = window.hide();
}

fn hide_webview_window(window: &tauri::WebviewWindow) {
    let _ = window.emit(WINDOW_VISIBILITY_EVENT, false);
    let _ = window.hide();
}

fn show_webview_window(window: &tauri::WebviewWindow) {
    let _ = window.show();
    let _ = window.emit(WINDOW_VISIBILITY_EVENT, true);
    let _ = window.set_focus();
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let http_client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .pool_max_idle_per_host(4)
        .build()
        .expect("failed to build HTTP client");

    tauri::Builder::default()
        .manage(commands::HttpClient(http_client))
        .manage(commands::Recorder(std::sync::Mutex::new(commands::RecorderState::new())))
        .manage(sck::SckRecorder(std::sync::Mutex::new(sck::SckRecorderState::new())))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![]),
        ))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        let toggle_window = Shortcut::new(
                            Some(Modifiers::SUPER | Modifiers::SHIFT),
                            Code::Space,
                        );
                        let toggle_recording = Shortcut::new(
                            Some(Modifiers::SUPER | Modifiers::SHIFT),
                            Code::KeyR,
                        );
                        if shortcut == &toggle_window {
                            if let Some(w) = app.get_webview_window("main") {
                                if w.is_visible().unwrap_or(false) {
                                    hide_webview_window(&w);
                                } else {
                                    show_webview_window(&w);
                                }
                            }
                        } else if shortcut == &toggle_recording {
                            let _ = app.emit("toggle-recording", ());
                        }
                    }
                })
                .build(),
        )
        .setup(|app| {
            // Register global shortcut: Cmd+Shift+Space (toggle window)
            let shortcut = Shortcut::new(
                Some(Modifiers::SUPER | Modifiers::SHIFT),
                Code::Space,
            );
            app.global_shortcut().register(shortcut)?;

            // Register global shortcut: Cmd+Shift+R (toggle recording)
            let record_shortcut = Shortcut::new(
                Some(Modifiers::SUPER | Modifiers::SHIFT),
                Code::KeyR,
            );
            app.global_shortcut().register(record_shortcut)?;

            // Build tray menu
            let show_i = MenuItem::with_id(app, "show", "Show Dashboard", true, None::<&str>)?;
            let quit_i = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = Menu::with_items(app, &[&show_i, &quit_i])?;
            // Build tray icon
            let _tray = TrayIconBuilder::new()
                .icon(include_image!("icons/tray-icon.png"))
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(w) = app.get_webview_window("main") {
                            show_webview_window(&w);
                        }
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = event
                    {
                        let app = tray.app_handle();
                        if let Some(w) = app.get_webview_window("main") {
                            if w.is_visible().unwrap_or(false) {
                                hide_webview_window(&w);
                            } else {
                                show_webview_window(&w);
                            }
                        }
                    }
                })
                .build(app)?;

            Ok(())
        })
        // Close hides window instead of quitting
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                hide_window(window);
                api.prevent_close();
            }
        })
        .invoke_handler(tauri::generate_handler![
            commands::check_health,
            commands::service_control,
            commands::read_log_tail,
            commands::get_services,
            commands::get_monorepo_root,
            commands::fetch_local_api,
            commands::get_audio_route_status,
            commands::restore_capture_audio_defaults,
            commands::get_microphone_permission_status,
            commands::request_microphone_permission,
            commands::get_recording_status,
            commands::start_recording,
            commands::stop_recording,
            commands::pause_recording,
            commands::resume_recording,
            commands::list_audio_input_devices,
            commands::get_recording_output_dir,
            commands::set_recording_output_dir,
            commands::reveal_recording_output_dir,
            commands::list_recordings,
            commands::delete_recording,
            commands::rename_recording,
            sck::get_screen_recording_permission_status,
            sck::open_screen_recording_settings,
            sck::list_capturable_apps,
            sck::start_sck_recording,
            sck::stop_sck_recording,
            sck::get_sck_recording_status,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                // Gracefully stop any active recording (ffmpeg + SCK) on app exit
                if let Some(recorder) = app.try_state::<commands::Recorder>() {
                    let sck_opt = app.try_state::<sck::SckRecorder>();
                    let sck_ref = sck_opt.as_ref().map(|s| &**s);
                    commands::stop_active_recording(&recorder, sck_ref);
                }
            }
        });
}
