//! Locating Blender.
//!
//! ArchX3D drives Blender as an external application rather than embedding it:
//! it is a ~500 MB GPL program that users install once and update on their own
//! schedule, and bundling a copy would multiply the installer's size while
//! taking on redistribution obligations for no functional gain.
//!
//! The search order mirrors `modules/render/renderer.py#blender_executable` so
//! the shell and the pipeline never disagree about which Blender is in use.

use std::path::PathBuf;

/// Install locations checked when `ARCHX3D_BLENDER` is unset, newest first.
const WINDOWS_GLOB_ROOTS: &[&str] = &[
    r"C:\Program Files\Blender Foundation",
    r"C:\Program Files (x86)\Blender Foundation",
];

/// The Blender the pipeline will use, or `None` if there is none to find.
pub fn find() -> Option<PathBuf> {
    if let Some(explicit) = std::env::var_os("ARCHX3D_BLENDER") {
        let path = PathBuf::from(explicit);
        if path.is_file() {
            return Some(path);
        }
    }

    let mut candidates: Vec<PathBuf> = Vec::new();
    for root in WINDOWS_GLOB_ROOTS {
        let Ok(entries) = std::fs::read_dir(root) else {
            continue;
        };
        for entry in entries.flatten() {
            let exe = entry.path().join("blender.exe");
            if exe.is_file() {
                candidates.push(exe);
            }
        }
    }

    // Highest version last in lexical order — "Blender 5.0" sorts after
    // "Blender 4.2", which is the one a user would expect to be picked.
    candidates.sort();
    candidates.pop()
}

/// Message shown when no Blender can be found.
pub fn missing_message() -> String {
    "ArchX3D could not find Blender on this computer.\n\n\
     Blender does the 3D generation, so plans can be uploaded and reviewed but \
     not built into a model until it is installed.\n\n\
     Install Blender (4.2 or newer) from blender.org, then restart ArchX3D. If \
     it is installed somewhere unusual, set the ARCHX3D_BLENDER environment \
     variable to the full path of blender.exe."
        .to_string()
}
