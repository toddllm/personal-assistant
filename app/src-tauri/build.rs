fn main() {
    // ScreenCaptureKit (via the screencapturekit crate) uses Swift concurrency.
    // Link against the system Swift runtime (macOS 12.3+ ships it at /usr/lib/swift).
    #[cfg(target_os = "macos")]
    {
        println!("cargo:rustc-link-search=native=/usr/lib/swift");
        println!("cargo:rustc-link-arg=-Wl,-rpath,/usr/lib/swift");
    }

    tauri_build::build()
}
