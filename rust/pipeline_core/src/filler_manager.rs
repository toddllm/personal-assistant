//! WAV loading and categorized filler clip selection.
//!
//! Loads WAV files from disk (via `hound`), resamples to 32kHz mono s16le,
//! and categorizes by duration: short (<1s), medium (1-2s), long (2-3s).

use pyo3::prelude::*;
use pyo3::types::{PyBytes, PyDict, PyList};
use rand::seq::SliceRandom;
use std::path::Path;

struct FillerClip {
    label: String,
    pcm_32k: Vec<u8>,
    duration_ms: f64,
    category: String,
}

#[pyclass]
pub struct FillerManager {
    clips: Vec<FillerClip>,
}

impl FillerManager {
    fn categorize(duration_ms: f64) -> String {
        if duration_ms < 1000.0 {
            "short".to_string()
        } else if duration_ms < 2000.0 {
            "medium".to_string()
        } else {
            "long".to_string()
        }
    }

    fn resample_to_32k_mono(samples: &[i16], spec: hound::WavSpec) -> Vec<u8> {
        // First: mix to mono if stereo
        let mono: Vec<i16> = if spec.channels > 1 {
            samples
                .chunks(spec.channels as usize)
                .map(|ch| {
                    let sum: i32 = ch.iter().map(|&s| s as i32).sum();
                    (sum / ch.len() as i32) as i16
                })
                .collect()
        } else {
            samples.to_vec()
        };

        // If already 32kHz, just pack
        if spec.sample_rate == 32000 {
            let mut out = Vec::with_capacity(mono.len() * 2);
            for &s in &mono {
                out.extend_from_slice(&s.to_le_bytes());
            }
            return out;
        }

        // Resample via linear interpolation
        let ratio = 32000.0_f64 / spec.sample_rate as f64;
        let out_len = (mono.len() as f64 * ratio) as usize;
        let mut result = Vec::with_capacity(out_len * 2);

        for i in 0..out_len {
            let src_idx = i as f64 / ratio;
            let idx = src_idx as usize;
            let frac = src_idx - idx as f64;
            let val = if idx + 1 < mono.len() {
                mono[idx] as f64 * (1.0 - frac) + mono[idx + 1] as f64 * frac
            } else if idx < mono.len() {
                mono[idx] as f64
            } else {
                0.0
            };
            let s = val.max(-32768.0).min(32767.0) as i16;
            result.extend_from_slice(&s.to_le_bytes());
        }

        result
    }
}

#[pymethods]
impl FillerManager {
    #[new]
    fn new() -> Self {
        FillerManager {
            clips: Vec::new(),
        }
    }

    /// Load a single WAV file, resample to 32kHz mono.
    fn load_wav(&mut self, path: &str) -> PyResult<()> {
        let reader = hound::WavReader::open(path)
            .map_err(|e| pyo3::exceptions::PyIOError::new_err(format!("Failed to open WAV: {}", e)))?;

        let spec = reader.spec();
        let samples: Vec<i16> = if spec.bits_per_sample == 16 {
            reader
                .into_samples::<i16>()
                .filter_map(|s| s.ok())
                .collect()
        } else {
            // Convert 24/32-bit to 16-bit
            let max = (1i32 << (spec.bits_per_sample - 1)) as f64;
            reader
                .into_samples::<i32>()
                .filter_map(|s| s.ok())
                .map(|s| ((s as f64 / max) * 32767.0).max(-32768.0).min(32767.0) as i16)
                .collect()
        };

        let pcm_32k = Self::resample_to_32k_mono(&samples, spec);
        let duration_ms = pcm_32k.len() as f64 / (32000.0 * 2.0) * 1000.0;
        let category = Self::categorize(duration_ms);
        let label = Path::new(path)
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| path.to_string());

        self.clips.push(FillerClip {
            label,
            pcm_32k,
            duration_ms,
            category,
        });

        Ok(())
    }

    /// Batch-load all .wav files from a directory. Returns count loaded.
    fn load_directory(&mut self, dir_path: &str) -> PyResult<usize> {
        let path = Path::new(dir_path);
        if !path.is_dir() {
            return Ok(0);
        }

        let mut count = 0;
        let entries: Vec<_> = std::fs::read_dir(path)
            .map_err(|e| pyo3::exceptions::PyIOError::new_err(format!("Failed to read dir: {}", e)))?
            .filter_map(|e| e.ok())
            .collect();

        for entry in entries {
            let file_path = entry.path();
            if let Some(ext) = file_path.extension() {
                if ext.to_str().unwrap_or("").to_lowercase() == "wav" {
                    if let Ok(()) = self.load_wav(file_path.to_str().unwrap_or("")) {
                        count += 1;
                    }
                }
            }
        }

        Ok(count)
    }

    /// Add a pre-rendered PCM clip (32kHz mono s16le) directly.
    fn add_pcm(&mut self, label: &str, pcm_32k: &[u8]) {
        let duration_ms = pcm_32k.len() as f64 / (32000.0 * 2.0) * 1000.0;
        let category = Self::categorize(duration_ms);
        self.clips.push(FillerClip {
            label: label.to_string(),
            pcm_32k: pcm_32k.to_vec(),
            duration_ms,
            category,
        });
    }

    /// Select a random clip from the given category. Returns None if no clips match.
    #[pyo3(signature = (category="short"))]
    fn select<'py>(&self, py: Python<'py>, category: &str) -> Option<Bound<'py, PyBytes>> {
        let matching: Vec<&FillerClip> = self
            .clips
            .iter()
            .filter(|c| c.category == category)
            .collect();

        if matching.is_empty() {
            // Fall back to any clip
            if self.clips.is_empty() {
                return None;
            }
            let clip = self.clips.choose(&mut rand::thread_rng())?;
            return Some(PyBytes::new(py, &clip.pcm_32k));
        }

        let clip = matching.choose(&mut rand::thread_rng())?;
        Some(PyBytes::new(py, &clip.pcm_32k))
    }

    /// List metadata for all loaded filler clips.
    fn list_fillers<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty(py);
        for clip in &self.clips {
            let dict = PyDict::new(py);
            dict.set_item("label", &clip.label)?;
            dict.set_item("duration_ms", clip.duration_ms)?;
            dict.set_item("category", &clip.category)?;
            dict.set_item("size_bytes", clip.pcm_32k.len())?;
            list.append(dict)?;
        }
        Ok(list)
    }

    fn __len__(&self) -> usize {
        self.clips.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_add_pcm_and_select() {
        Python::with_gil(|py| {
            let mut mgr = FillerManager::new();
            // Short clip: 0.5s at 32kHz mono = 32000 bytes
            let pcm = vec![0u8; 32000];
            mgr.add_pcm("test_short", &pcm);
            assert_eq!(mgr.__len__(), 1);

            let clip = mgr.select(py, "short");
            assert!(clip.is_some());
        });
    }

    #[test]
    fn test_categorize() {
        assert_eq!(FillerManager::categorize(500.0), "short");
        assert_eq!(FillerManager::categorize(1500.0), "medium");
        assert_eq!(FillerManager::categorize(2500.0), "long");
    }

    #[test]
    fn test_empty_select() {
        Python::with_gil(|py| {
            let mgr = FillerManager::new();
            assert!(mgr.select(py, "short").is_none());
        });
    }

    #[test]
    fn test_list_fillers() {
        Python::with_gil(|py| {
            let mut mgr = FillerManager::new();
            mgr.add_pcm("hello", &vec![0u8; 64000]); // 1s = medium
            let fillers = mgr.list_fillers(py).unwrap();
            assert_eq!(fillers.len(), 1);
        });
    }
}
