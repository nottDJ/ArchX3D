//! ArchX3D desktop shell.
//!
//! A thin native wrapper: start the bundled backend, tell the web frontend
//! which port it landed on, and show it in a window. All the product logic
//! stays in the Python pipeline and the Next.js app, which are the same code
//! the browser-based workflow runs.

mod backend;
mod blender;

use std::sync::Arc;

use tauri::{Manager, WindowEvent};
use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};

/// Reported to the UI so it can say what is and is not available.
#[derive(serde::Serialize)]
pub struct Environment {
    api_base_url: String,
    blender_path: Option<String>,
    data_root: String,
}

#[tauri::command]
fn environment(state: tauri::State<'_, AppState>) -> Environment {
    Environment {
        api_base_url: state.backend.base_url.clone(),
        blender_path: state
            .blender
            .as_ref()
            .map(|p| p.to_string_lossy().into_owned()),
        data_root: state.data_root.clone(),
    }
}

pub struct AppState {
    backend: Arc<backend::Backend>,
    blender: Option<std::path::PathBuf>,
    data_root: String,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![environment])
        .setup(|app| {
            let resource_dir = app
                .path()
                .resource_dir()
                .map_err(|e| format!("Could not locate the application resources: {e}"))?;
            let data_root = backend::data_root();

            let backend = match backend::Backend::start(&resource_dir, &data_root) {
                Ok(backend) => Arc::new(backend),
                Err(message) => {
                    // Nothing works without the backend, so this is fatal —
                    // but it must be *explained* rather than silently exiting
                    // to a window that fails every request.
                    app.dialog()
                        .message(&message)
                        .kind(MessageDialogKind::Error)
                        .title("ArchX3D could not start")
                        .buttons(MessageDialogButtons::Ok)
                        .blocking_show();
                    std::process::exit(1);
                }
            };

            let blender = blender::find();
            if blender.is_none() {
                // Not fatal: upload, analysis and review all work without
                // Blender. Only generation needs it, so the app opens and says
                // so rather than refusing to run.
                app.dialog()
                    .message(blender::missing_message())
                    .kind(MessageDialogKind::Warning)
                    .title("Blender not found")
                    .buttons(MessageDialogButtons::Ok)
                    .blocking_show();
            }

            // Hand the frontend its API origin before any page script runs.
            // `lib/api.ts` reads this global; without it the static build would
            // fall back to its compile-time default and miss the live port.
            let init = format!(
                "window.__ARCHX3D_API_BASE_URL__ = {};",
                serde_json::to_string(&backend.base_url).unwrap_or_else(|_| "\"\"".into())
            );

            // Built here rather than declared in tauri.conf.json: the API base
            // URL is not known until the backend has claimed a port, and the
            // injection has to happen before any page script runs. Declaring a
            // window in the config *as well* would panic on the duplicate
            // `main` label, which is why `app.windows` there is empty.
            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::App("index.html".into()),
            )
            .title("ArchX3D")
            .inner_size(1400.0, 900.0)
            .min_inner_size(900.0, 600.0)
            .center()
            .initialization_script(&init)
            .build()?;

            // The backend is a separate OS process; closing the window must
            // take it with us or it keeps running headless after the app is
            // gone, holding its port and its data directory.
            let on_close = backend.clone();
            window.on_window_event(move |event| {
                if matches!(event, WindowEvent::Destroyed) {
                    on_close.stop();
                }
            });

            app.manage(AppState {
                backend,
                blender,
                data_root: data_root.to_string_lossy().into_owned(),
            });

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the ArchX3D application")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(state) = app_handle.try_state::<AppState>() {
                    state.backend.stop();
                }
            }
        });
}
