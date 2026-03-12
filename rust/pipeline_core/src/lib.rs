//! pipeline_core — Rust-accelerated audio processing for the Discord voice bot.
//!
//! Provides:
//! - Fast PCM resampling (4 functions)
//! - Thread-safe AudioBuffer for TTS playback
//! - Text-based EchoDetector for echo suppression
//! - FillerManager for WAV loading and categorized selection

mod audio_buffer;
mod echo_detector;
mod filler_manager;
mod resample;

use pyo3::prelude::*;

/// Python module: `import pipeline_core`
#[pymodule]
fn pipeline_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // Resampling functions
    m.add_function(wrap_pyfunction!(resample::resample_48k_stereo_to_32k_mono, m)?)?;
    m.add_function(wrap_pyfunction!(resample::resample_32k_to_16k, m)?)?;
    m.add_function(wrap_pyfunction!(resample::resample_to_32k, m)?)?;
    m.add_function(wrap_pyfunction!(resample::resample_32k_to_48k_stereo, m)?)?;

    // Classes
    m.add_class::<audio_buffer::AudioBuffer>()?;
    m.add_class::<echo_detector::EchoDetector>()?;
    m.add_class::<filler_manager::FillerManager>()?;

    Ok(())
}
