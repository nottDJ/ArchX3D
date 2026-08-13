# ArchX3D — Desktop application

How the installable Windows app is put together, how to build it, and what it
deliberately does not include.

---

## Contents

1. [What it is](#1-what-it-is)
2. [Architecture](#2-architecture)
3. [What runs where](#3-what-runs-where)
4. [Building](#4-building)
5. [Prerequisites](#5-prerequisites)
6. [Blender](#6-blender)
7. [Where data lives](#7-where-data-lives)
8. [Design decisions](#8-design-decisions)
9. [Troubleshooting](#9-troubleshooting)
10. [Known limitations](#10-known-limitations)

---

## 1. What it is

A native Windows application that bundles the *same* Next.js frontend and
FastAPI/Python pipeline the browser workflow uses. Nothing was rewritten for
the desktop: the shell starts the backend, points a webview at the frontend,
and gets out of the way.

The user installs one `.exe`, gets a Start Menu entry, and needs **no Python,
no Node and no terminal**.

```
ArchX3D_2.0.0_x64-setup.exe      ~97 MB installer
  └─ installs ~390 MB            Python runtime + pipeline + frontend
```

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│ archx3d-desktop.exe            Tauri shell (Rust)           │
│                                                             │
│  • picks a free TCP port                                    │
│  • starts the backend, waits for it to answer               │
│  • injects the port into the page before any script runs    │
│  • kills the backend when the app closes — or crashes       │
│                                                             │
│  ┌───────────────────────┐    ┌──────────────────────────┐  │
│  │ WebView2              │    │ archx3d-backend.exe      │  │
│  │ (the static frontend) │───▶│ (frozen Python)          │  │
│  │  web/out/**           │    │  FastAPI + the pipeline  │  │
│  └───────────────────────┘    └───────────┬──────────────┘  │
└─────────────────────────────────────────────┼───────────────┘
                                              │ spawns
                                              ▼
                                   ┌──────────────────────┐
                                   │ blender.exe          │
                                   │ (found, not bundled) │
                                   └──────────────────────┘
```

### The three build artefacts

| Artefact | Produced by | Contents |
| --- | --- | --- |
| `web/out/` | `npm run build` | Static HTML/JS/CSS. No Node at runtime. |
| `desktop/dist/archx3d-backend/` | PyInstaller | Python 3.11, FastAPI, ezdxf, numpy, scipy, OpenCV, the whole `modules/` tree. |
| `…/bundle/nsis/*.exe` | `cargo tauri build` | The installer wrapping both. |

---

## 3. What runs where

**One binary, seven entry points.** The pipeline is a chain of subprocesses —
`server` spawns `main.py`, which spawns `dxf_extractor.py`, `scene_analyzer.py`
and the rest. Freezing each separately would duplicate numpy, OpenCV and ezdxf
seven times. Instead `archx3d-backend.exe` re-invokes *itself*:

```
archx3d-backend.exe                          → serve the API
archx3d-backend.exe --child main …           → the CLI pipeline
archx3d-backend.exe --child dxf_extractor …  → one stage
```

[`modules/child_process.py`](../modules/child_process.py) builds the argv and
holds the script→module mapping; [`desktop/backend_main.py`](../desktop/backend_main.py)
dispatches it. A source checkout is unaffected — it still spawns a plain
interpreter, and `sys.frozen` is what switches between them.

Adding a pipeline stage that gets spawned means adding one line to
`CHILD_MODULES`. Forgetting raises a clear `KeyError` naming the file, rather
than failing mysteriously inside the child.

---

## 4. Building

```powershell
.\desktop\build.ps1                 # everything, ~6-8 min cold
.\desktop\build.ps1 -SkipBackend    # frontend + installer only, ~2 min
.\desktop\build.ps1 -SkipFrontend -SkipBackend   # shell only, ~1 min
```

The script runs the three stages in dependency order and fails loudly if an
earlier one did not produce its output. The installer lands in:

```
desktop/src-tauri/target/release/bundle/nsis/ArchX3D_<version>_x64-setup.exe
```

### Doing it by hand

```powershell
cd web;    npm ci; npm run build            # 1. static frontend
cd ..;     .venv-build\Scripts\python.exe -m PyInstaller `
             desktop\archx3d-backend.spec --noconfirm `
             --distpath desktop\dist --workpath desktop\build   # 2. backend
cd desktop\src-tauri; cargo tauri build      # 3. installer
```

---

## 5. Prerequisites

Only for *building*. Users of the installer need none of this.

| Tool | Why | Notes |
| --- | --- | --- |
| Node 18+ | frontend | |
| Python 3.11 | backend | The bundled runtime is whatever builds it. |
| Rust (stable, MSVC) | shell | `winget install Rustlang.Rustup` |
| Visual Studio Build Tools | Rust links with `link.exe` | Needs the **C++ build tools** *and* the **Windows SDK** — the SDK is a separate component and its absence shows up as `LNK1181: cannot open input file 'kernel32.lib'`. |
| WebView2 runtime | the webview | Preinstalled on Windows 11 and current Windows 10. |

**Build in a virtualenv.** `desktop/build.ps1` creates `.venv-build/` and
installs `requirements.txt` there. PyInstaller bundles whatever it can see, so
building from a global interpreter drags unrelated packages into the installer
— and a stray `pathlib` backport in a global environment makes PyInstaller
refuse to run at all.

---

## 6. Blender

**Blender is found, not bundled.** It is a ~500 MB GPL application that users
install once and update themselves; shipping a copy would multiply the
installer's size and take on redistribution obligations for no functional gain.

The shell searches the same places the pipeline does
([`modules/render/renderer.py`](../modules/render/renderer.py)):

1. `ARCHX3D_BLENDER`, if set
2. `C:\Program Files\Blender Foundation\Blender *\blender.exe` — newest wins
3. the x86 Program Files equivalent

If none is found the app **still opens** and says so: uploading, analysis and
review all work without Blender. Only generation needs it.

---

## 7. Where data lives

| | Path |
| --- | --- |
| Program | `C:\Program Files\ArchX3D\` (read-only) |
| Projects, outputs, uploads | `%LOCALAPPDATA%\ArchX3D\` |

The split is not cosmetic. Inside a PyInstaller bundle the code lives in a temp
directory that is deleted when the process exits, so writing outputs beside the
code would silently discard every generated model on close.
[`modules/app_paths.py`](../modules/app_paths.py) resolves the two separately
and publishes the data root as `ARCHX3D_DATA_ROOT` so every child process —
including Blender's own interpreter, which cannot import our modules — agrees
with its parent.

Uninstalling leaves `%LOCALAPPDATA%\ArchX3D\` in place. Projects are the user's
work, not the program's.

---

## 8. Design decisions

### The port is chosen at runtime, not compiled in

The backend binds port 0, the OS assigns a free one, and the shell injects the
result as `window.__ARCHX3D_API_BASE_URL__` before any page script runs.
[`web/lib/api.ts`](../web/lib/api.ts) prefers that global over its build-time
constant.

A fixed 8000 would fail whenever a browser-mode instance, a second copy of the
app, or an unrelated service already held it — and the frontend is a *static*
bundle, so it cannot be told the port at build time either.

### The backend dies with the app, guaranteed

Graceful paths (`WindowEvent::Destroyed`, `RunEvent::ExitRequested`) call
`stop()`. Neither runs when the process is killed or crashes, so the child is
also placed in a **Win32 job object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`:
Windows itself terminates it when the last handle closes, which process exit
always does.

This was a real bug found in testing — force-killing the shell left a backend
holding its port and data directory, which makes the *next* launch look
corrupted.

### The webview's origin must be in the CORS allow-list

The desktop build is a *browser*, not a terminal: it enforces the same-origin
policy, and its pages load from Tauri's own scheme
(`http://tauri.localhost` on Windows, `tauri://localhost` elsewhere) rather
than from the API's origin. Both are in `server.py`'s `allow_origins`.

This was missed until the app was actually opened, because every test up to
that point used `curl` — **which ignores CORS entirely**. An identical request
succeeded from the shell and failed from the window. Any future change to the
API surface should be checked from the app, not only from a terminal.

### The frontend is rebuilt by the Tauri build, not before it

`beforeBuildCommand` runs `npm run build` in `web/`, so `web/out` can never lag
behind the source. It was briefly empty during development, and the resulting
installer shipped a stale bundle — the app fell back to the compile-time
`localhost:8000` because the runtime-injection code was not in the JS yet. The
symptom (an app that cannot reach its own backend) pointed nowhere near the
cause, which is why the build now owns the dependency.

Note the working directory: `beforeBuildCommand` runs from `desktop/`, the
parent of `src-tauri/` — not from `src-tauri/` itself.

### The Gemini key can be entered in the app

Everything that calls Gemini reads `GEMINI_API_KEY` from the environment, which
is right for a developer running the CLI and useless for someone who installed
an `.exe` — they would have to add a Windows environment variable before the AI
features did anything, and until then the pipeline would quietly produce
unfurnished shells with no indication why.

So **Settings → AI analysis** writes the key to
`%LOCALAPPDATA%\ArchX3D\credentials.json`, and `modules/credentials.py` is the
one place that knows about both sources. An environment-supplied key always
wins; the file is a fallback, not an authority.

Two details worth knowing:

- **The key is never sent back to the browser.** The status endpoint returns
  whether one is configured, where it came from, and a masked hint
  (`AIza…7f3D`) — enough to tell *which* key is saved, useless to anyone
  reading it.
- **It is stored in plain text**, protected by the Windows account boundary and
  nothing else. Encrypting it would require a decryption key stored beside it,
  which is theatre rather than security. On a shared machine, use the
  environment variable.

The subtlety that cost a bug: saving a key also exports it to `os.environ` so
the pipeline's subprocesses inherit it. Deciding "did the user configure this
externally?" by then reading `os.environ` answers *yes* for a key the UI just
saved — so the UI locked itself out of editing its own setting. Only the value
present at **process start** is evidence of an external key, which is why
`credentials.py` captures it at import and `externally_set()` exists.

### `onedir`, not `onefile`

A `onefile` PyInstaller build extracts ~390 MB to a temp directory on **every**
launch — tens of seconds before the window appears. `onedir` extracts once, at
install time.

### Static export, and what it cost

`output: "export"` means no Node server at runtime. Two routes had to change:

- `/viewer` read `searchParams` in an async Server Component → now
  `useSearchParams` in a client component.
- `/generate/[job_id]` used a dynamic path segment and `force-dynamic`, neither
  of which a static export can pre-render → now `/generate?job_id=…`, matching
  the query-parameter convention `/viewer` and `/compare` already used.

Nothing linked to the old URL internally, so no call sites changed.

---

## 9. Troubleshooting

**"ArchX3D could not start" on first launch.**
Windows Defender scans ~390 MB of freshly written DLLs the first time. The
shell waits 90 s; if it times out, opening the app again is usually instant.

**"Blender not found".**
Install Blender 4.2+ from blender.org, or set `ARCHX3D_BLENDER` to the full
path of `blender.exe` and restart.

**`LNK1181: cannot open input file 'kernel32.lib'` when building.**
The Windows SDK is missing. Visual Studio Installer → Modify → Individual
Components → **Windows 11 SDK**.

**PyInstaller refuses to run: "the 'pathlib' package is obsolete".**
A `pathlib` backport is installed in that interpreter. Build in the virtualenv
(`desktop/build.ps1` does this) rather than uninstalling it globally — another
package may depend on it.

**Generation fails but analysis works.**
Almost always Blender. Check the log panel for the `blender.exe` command line.

**"Cannot reach the ArchX3D server at http://localhost:8000".**
That address is the *browser* build's compile-time default, so seeing it inside
the desktop app means the runtime port injection did not reach the page — the
bundled frontend is stale. Rebuild with `cargo tauri build` (which now rebuilds
`web/out` itself) rather than reusing an old `web/out`.

A different address in the same message means the backend started but the
request was rejected — check that the webview's origin is in `server.py`'s
`allow_origins`.

---

## 10. Known limitations

- **Windows only.** The shell's process supervision uses job objects and the
  Blender search knows Windows paths. Tauri itself is cross-platform, so macOS
  and Linux are a matter of writing those two pieces.
- **Unsigned.** No code-signing certificate, so SmartScreen shows "Windows
  protected your PC" on first run (More info → Run anyway). Signing needs a
  certificate, not a code change.
- **No auto-update.** Tauri's updater needs a signing key and a release feed.
- **Installer size is dominated by Python.** ~390 MB installed, of which the
  Rust shell is ~10 MB; numpy, scipy and OpenCV are most of the rest.
- **The desktop app does not make the pipeline faster.** Analysis of a large
  plan takes the same minutes it takes in the browser — that cost is Blender,
  CAD geometry and the vision model, none of which the shell touches.

---

## Related

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the target architecture, which names
  the desktop app as one of five deployment shapes
- [`FRONTEND_ARCHITECTURE.md`](FRONTEND_ARCHITECTURE.md) — routing and state
- [`RENDER_PIPELINE.md`](RENDER_PIPELINE.md) — what Blender is asked to do
