fn main() {
    // ScreenCaptureKit (via the screencapturekit crate) uses Swift concurrency.
    // Find the Xcode toolchain's Swift runtime directory and add it as an rpath.
    #[cfg(target_os = "macos")]
    {
        if let Ok(output) = std::process::Command::new("xcrun")
            .args(["--toolchain", "default", "--find", "swift"])
            .output()
        {
            if let Ok(swift_path) = String::from_utf8(output.stdout) {
                let swift_bin = std::path::Path::new(swift_path.trim());
                if let Some(toolchain) = swift_bin.parent().and_then(|p| p.parent()) {
                    // Swift concurrency lives in lib/swift-5.5/macosx
                    for subdir in ["lib/swift-5.5/macosx", "lib/swift/macosx"] {
                        let lib_dir = toolchain.join(subdir);
                        if lib_dir.exists() {
                            println!("cargo:rustc-link-search=native={}", lib_dir.display());
                            println!("cargo:rustc-link-arg=-Wl,-rpath,{}", lib_dir.display());
                        }
                    }
                }
            }
        }
    }

    tauri_build::build()
}
