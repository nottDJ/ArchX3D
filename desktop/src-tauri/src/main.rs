// Release builds attach no console: the shell is a GUI, and a terminal window
// opening behind it would look like a bug. Debug builds keep it for logs.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    archx3d_desktop_lib::run()
}
