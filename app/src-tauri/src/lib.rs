mod commands;

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
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_autostart::init(
            MacosLauncher::LaunchAgent,
            Some(vec![]),
        ))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        let s = Shortcut::new(
                            Some(Modifiers::SUPER | Modifiers::SHIFT),
                            Code::Space,
                        );
                        if shortcut == &s {
                            if let Some(w) = app.get_webview_window("main") {
                                if w.is_visible().unwrap_or(false) {
                                    hide_webview_window(&w);
                                } else {
                                    show_webview_window(&w);
                                }
                            }
                        }
                    }
                })
                .build(),
        )
        .setup(|app| {
            // Register global shortcut: Cmd+Shift+Space
            let shortcut = Shortcut::new(
                Some(Modifiers::SUPER | Modifiers::SHIFT),
                Code::Space,
            );
            app.global_shortcut().register(shortcut)?;

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
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
