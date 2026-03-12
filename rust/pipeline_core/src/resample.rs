//! Drop-in replacements for the Python resampling functions.
//!
//! All operate on `&[u8]` (s16le PCM), return `PyBytes`.
//! Process samples as `i16`/`f64` in tight loops, no per-sample allocation.

use pyo3::prelude::*;
use pyo3::types::PyBytes;

/// Parse s16le bytes into a Vec<i16>.
#[inline]
fn bytes_to_samples(data: &[u8]) -> Vec<i16> {
    data.chunks_exact(2)
        .map(|c| i16::from_le_bytes([c[0], c[1]]))
        .collect()
}

/// Pack i16 samples into s16le bytes.
#[inline]
fn samples_to_bytes(samples: &[i16]) -> Vec<u8> {
    let mut out = Vec::with_capacity(samples.len() * 2);
    for &s in samples {
        out.extend_from_slice(&s.to_le_bytes());
    }
    out
}

/// Clamp an f64 to i16 range.
#[inline]
fn clamp_i16(v: f64) -> i16 {
    if v > 32767.0 {
        32767
    } else if v < -32768.0 {
        -32768
    } else {
        v as i16
    }
}

/// Resample 48kHz stereo s16le to 32kHz mono via linear interpolation.
///
/// Replaces `audio_sink.py:resample_48k_stereo_to_32k_mono`.
#[pyfunction]
pub fn resample_48k_stereo_to_32k_mono<'py>(
    py: Python<'py>,
    pcm_48k_stereo: &[u8],
) -> Bound<'py, PyBytes> {
    let n_frames = pcm_48k_stereo.len() / 4;
    if n_frames == 0 {
        return PyBytes::new(py, &[]);
    }

    // Parse stereo interleaved samples
    let stereo = bytes_to_samples(&pcm_48k_stereo[..n_frames * 4]);

    // Mix to mono (average L+R)
    let mut mono_48k = Vec::with_capacity(n_frames);
    for i in 0..n_frames {
        let left = stereo[i * 2] as i32;
        let right = stereo[i * 2 + 1] as i32;
        mono_48k.push(((left + right) / 2) as i16);
    }

    // Resample 48kHz -> 32kHz via linear interpolation
    let ratio = 32000.0_f64 / 48000.0;
    let out_len = (mono_48k.len() as f64 * ratio) as usize;
    let mut result = Vec::with_capacity(out_len);

    for i in 0..out_len {
        let src_idx = i as f64 / ratio;
        let idx = src_idx as usize;
        let frac = src_idx - idx as f64;
        let val = if idx + 1 < mono_48k.len() {
            mono_48k[idx] as f64 * (1.0 - frac) + mono_48k[idx + 1] as f64 * frac
        } else {
            mono_48k[idx.min(mono_48k.len() - 1)] as f64
        };
        result.push(clamp_i16(val));
    }

    let bytes = samples_to_bytes(&result);
    PyBytes::new(py, &bytes)
}

/// Downsample 32kHz mono s16le to 16kHz by taking every other sample.
///
/// Replaces `audio_pipeline.py:resample_32k_to_16k`.
#[pyfunction]
pub fn resample_32k_to_16k<'py>(py: Python<'py>, pcm_32k: &[u8]) -> Bound<'py, PyBytes> {
    let samples = bytes_to_samples(pcm_32k);
    let downsampled: Vec<i16> = samples.iter().step_by(2).copied().collect();
    let bytes = samples_to_bytes(&downsampled);
    PyBytes::new(py, &bytes)
}

/// Resample any-rate mono s16le to 32kHz via linear interpolation.
///
/// Replaces `audio_pipeline.py:resample_to_32k`.
#[pyfunction]
pub fn resample_to_32k<'py>(
    py: Python<'py>,
    pcm_data: &[u8],
    source_rate: u32,
) -> Bound<'py, PyBytes> {
    if source_rate == 32000 {
        return PyBytes::new(py, pcm_data);
    }

    // Ensure even length
    let len = pcm_data.len() & !1;
    let samples = bytes_to_samples(&pcm_data[..len]);
    if samples.is_empty() {
        return PyBytes::new(py, &[]);
    }

    let ratio = 32000.0_f64 / source_rate as f64;
    let out_len = (samples.len() as f64 * ratio) as usize;
    let mut result = Vec::with_capacity(out_len);

    for i in 0..out_len {
        let src_idx = i as f64 / ratio;
        let idx = src_idx as usize;
        let frac = src_idx - idx as f64;
        let val = if idx + 1 < samples.len() {
            samples[idx] as f64 * (1.0 - frac) + samples[idx + 1] as f64 * frac
        } else if idx < samples.len() {
            samples[idx] as f64
        } else {
            0.0
        };
        result.push(clamp_i16(val));
    }

    let bytes = samples_to_bytes(&result);
    PyBytes::new(py, &bytes)
}

/// Resample 32kHz mono s16le to 48kHz stereo with volume scaling.
///
/// Replaces `audio_source.py:resample_32k_to_48k`.
#[pyfunction]
pub fn resample_32k_to_48k_stereo<'py>(
    py: Python<'py>,
    pcm_32k: &[u8],
    volume: f64,
) -> Bound<'py, PyBytes> {
    let n_samples = pcm_32k.len() / 2;
    if n_samples == 0 {
        return PyBytes::new(py, &[]);
    }

    let samples = bytes_to_samples(&pcm_32k[..n_samples * 2]);
    let ratio = 48000.0_f64 / 32000.0;
    let out_len = (n_samples as f64 * ratio) as usize;

    // Output is stereo: each sample duplicated to L+R
    let mut result = Vec::with_capacity(out_len * 2);

    for i in 0..out_len {
        let src_idx = i as f64 / ratio;
        let idx = src_idx as usize;
        let frac = src_idx - idx as f64;
        let val = if idx + 1 < n_samples {
            samples[idx] as f64 * (1.0 - frac) + samples[idx + 1] as f64 * frac
        } else {
            samples[idx.min(n_samples - 1)] as f64
        };
        let s = clamp_i16(val * volume);
        // Stereo: duplicate to L and R
        result.push(s);
        result.push(s);
    }

    let bytes = samples_to_bytes(&result);
    PyBytes::new(py, &bytes)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_resample_48k_stereo_to_32k_mono_empty() {
        Python::with_gil(|py| {
            let result = resample_48k_stereo_to_32k_mono(py, &[]);
            assert_eq!(result.as_bytes().len(), 0);
        });
    }

    #[test]
    fn test_resample_32k_to_16k_decimation() {
        Python::with_gil(|py| {
            // 4 samples at 32k -> 2 samples at 16k
            let input: Vec<u8> = [100i16, 200, 300, 400]
                .iter()
                .flat_map(|s| s.to_le_bytes())
                .collect();
            let result = resample_32k_to_16k(py, &input);
            let bytes = result.as_bytes();
            assert_eq!(bytes.len(), 4); // 2 samples * 2 bytes
            let s0 = i16::from_le_bytes([bytes[0], bytes[1]]);
            let s1 = i16::from_le_bytes([bytes[2], bytes[3]]);
            assert_eq!(s0, 100);
            assert_eq!(s1, 300);
        });
    }

    #[test]
    fn test_resample_to_32k_passthrough() {
        Python::with_gil(|py| {
            let input = vec![1u8, 2, 3, 4];
            let result = resample_to_32k(py, &input, 32000);
            assert_eq!(result.as_bytes(), &input[..]);
        });
    }

    #[test]
    fn test_resample_32k_to_48k_stereo_volume() {
        Python::with_gil(|py| {
            // Single sample = 1000
            let input: Vec<u8> = 1000i16.to_le_bytes().to_vec();
            let result = resample_32k_to_48k_stereo(py, &input, 0.5);
            let bytes = result.as_bytes();
            // Should produce stereo samples (each sample duplicated)
            assert!(bytes.len() >= 4); // At least 1 stereo pair
            // First stereo pair: should be ~500 (1000 * 0.5)
            let s0 = i16::from_le_bytes([bytes[0], bytes[1]]);
            assert!((s0 - 500).abs() <= 1);
        });
    }
}
