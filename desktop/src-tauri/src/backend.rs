//! Supervises the bundled Python backend.
//!
//! The backend is a PyInstaller `onedir` bundle shipped as a Tauri resource
//! (`resources/backend/archx3d-backend.exe`). This module owns its whole
//! lifetime: pick a port, start it, wait until it answers, and make sure it
//! dies with the app.
//!
//! Why a child process and not an embedded interpreter
//! ---------------------------------------------------
//! The pipeline already spawns itself repeatedly (Blender, the extractor, the
//! analyser) and expects to be a normal OS process with a writable working
//! directory. Embedding CPython in the Rust binary would buy nothing and break
//! all of that.

use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;

/// Windows `CREATE_NO_WINDOW`: the backend is a console-subsystem binary, so
/// without this every launch flashes a terminal beside the app window.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

/// How long the backend may take to answer before we call it dead.
///
/// Generous because the first launch pays for Windows Defender scanning a
/// ~390 MB tree of freshly written DLLs, which on a cold cache genuinely takes
/// tens of seconds. A user seeing "starting…" is better than a false failure.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(90);

pub struct Backend {
    child: Mutex<Option<Child>>,
    pub base_url: String,
}

impl Backend {
    /// Start the backend and wait until its health endpoint answers.
    pub fn start(resource_dir: &Path, data_root: &Path) -> Result<Self, String> {
        let exe = resource_dir.join("backend").join("archx3d-backend.exe");
        if !exe.exists() {
            return Err(format!(
                "The ArchX3D backend is missing from this installation.\n\n\
                 Expected it at:\n{}\n\n\
                 Reinstalling should restore it.",
                exe.display()
            ));
        }

        std::fs::create_dir_all(data_root)
            .map_err(|e| format!("Could not create the data folder at {}: {e}", data_root.display()))?;

        let port = free_port()?;
        let base_url = format!("http://127.0.0.1:{port}");

        let mut command = Command::new(&exe);
        command
            .env("ARCHX3D_HOST", "127.0.0.1")
            .env("ARCHX3D_PORT", port.to_string())
            .env("ARCHX3D_DATA_ROOT", data_root)
            // The bundle's own directory is where main.py, config.json and the
            // pipeline modules live; the backend resolves them from _MEIPASS,
            // but its cwd should still be somewhere writable.
            .current_dir(data_root)
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());

        #[cfg(windows)]
        command.creation_flags(CREATE_NO_WINDOW);

        let mut child = command
            .spawn()
            .map_err(|e| format!("Could not start the ArchX3D backend: {e}"))?;

        // Tie the child's lifetime to ours at the OS level. The graceful paths
        // (window closed, exit requested) call stop() and are faster, but they
        // do not run when this process is killed or crashes — and a stranded
        // backend keeps a port and a lock on the data directory, so the next
        // launch misbehaves in a way that looks like corruption.
        #[cfg(windows)]
        kill_child_with_parent(&child);

        // Drain both pipes. A child whose stdout fills its pipe buffer blocks
        // forever, and uvicorn is chatty enough to reach that during a long
        // analysis run — so this is required for correctness, not just logging.
        for (label, pipe) in [
            ("out", child.stdout.take().map(Pipe::Out)),
            ("err", child.stderr.take().map(Pipe::Err)),
        ] {
            if let Some(pipe) = pipe {
                std::thread::spawn(move || match pipe {
                    Pipe::Out(handle) => drain(label, handle),
                    Pipe::Err(handle) => drain(label, handle),
                });
            }
        }

        let backend = Backend {
            child: Mutex::new(Some(child)),
            base_url: base_url.clone(),
        };

        backend.wait_until_healthy()?;
        Ok(backend)
    }

    /// Poll `/api/health` until it answers or the deadline passes.
    ///
    /// A TCP connect is enough: the port only opens once uvicorn has bound it,
    /// which is exactly the moment the API becomes reachable. Doing a real
    /// HTTP request would mean pulling in an HTTP client for one check.
    fn wait_until_healthy(&self) -> Result<(), String> {
        let port: u16 = self
            .base_url
            .rsplit(':')
            .next()
            .and_then(|p| p.parse().ok())
            .ok_or_else(|| "internal error: malformed backend URL".to_string())?;

        let deadline = Instant::now() + STARTUP_TIMEOUT;
        while Instant::now() < deadline {
            // If the process died there is no point waiting for the timeout.
            if let Ok(mut guard) = self.child.lock() {
                if let Some(child) = guard.as_mut() {
                    if let Ok(Some(status)) = child.try_wait() {
                        return Err(format!(
                            "The ArchX3D backend stopped unexpectedly during startup ({status}).\n\n\
                             This usually means the installation is incomplete or an \
                             antivirus removed part of it."
                        ));
                    }
                }
            }

            if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
                return Ok(());
            }
            std::thread::sleep(Duration::from_millis(200));
        }

        Err(format!(
            "The ArchX3D backend did not start within {} seconds.\n\n\
             If this is the first launch after installing, antivirus scanning \
             can delay it; try opening the app again.",
            STARTUP_TIMEOUT.as_secs()
        ))
    }

    /// Stop the backend. Safe to call more than once.
    pub fn stop(&self) {
        if let Ok(mut guard) = self.child.lock() {
            if let Some(mut child) = guard.take() {
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

impl Drop for Backend {
    fn drop(&mut self) {
        self.stop();
    }
}

/// Put the child in a job object that dies with this process.
///
/// `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` terminates every process in the job
/// once the last handle to it closes — and process exit closes our handles no
/// matter how the exit happened. The handle is deliberately never closed here:
/// it must outlive every normal code path, and the OS reclaims it at exit.
///
/// Failures are ignored on purpose. This is a belt-and-braces guarantee on top
/// of the explicit `stop()` calls; refusing to launch because a job object
/// could not be created would trade a rare stranded process for a broken app.
#[cfg(windows)]
fn kill_child_with_parent(child: &Child) {
    use windows::Win32::Foundation::HANDLE;
    use windows::Win32::System::JobObjects::{
        AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject,
        JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
    };

    unsafe {
        let Ok(job) = CreateJobObjectW(None, None) else {
            return;
        };

        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;

        if SetInformationJobObject(
            job,
            JobObjectExtendedLimitInformation,
            &limits as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32,
        )
        .is_err()
        {
            return;
        }

        let _ = AssignProcessToJobObject(job, HANDLE(child.as_raw_handle() as _));
        // `job` is intentionally leaked; see the doc comment.
    }
}

enum Pipe {
    Out(std::process::ChildStdout),
    Err(std::process::ChildStderr),
}

fn drain<R: std::io::Read>(label: &str, handle: R) {
    let reader = BufReader::new(handle);
    for line in reader.lines().map_while(Result::ok) {
        println!("[backend:{label}] {line}");
    }
}

/// Ask the OS for an unused port.
///
/// Binding to port 0 and reading back the assignment is the only way to get one
/// without a race; a hard-coded 8000 fails whenever a browser-mode instance,
/// another copy of the app, or an unrelated service already holds it.
fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0")
        .map_err(|e| format!("Could not reserve a local port for the backend: {e}"))?;
    let port = listener
        .local_addr()
        .map_err(|e| format!("Could not read back the reserved port: {e}"))?
        .port();
    drop(listener);
    Ok(port)
}

/// Where user data lives, outside the read-only install directory.
pub fn data_root() -> PathBuf {
    if let Some(local) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(local).join("ArchX3D");
    }
    std::env::temp_dir().join("ArchX3D")
}
