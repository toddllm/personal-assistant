//! Thread-safe audio buffer for TTS playback.
//!
//! Replaces `TTSAudioSource`'s `bytearray` + `threading.Lock`.

use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::VecDeque;
use std::sync::Mutex;

#[pyclass]
pub struct AudioBuffer {
    buffer: Mutex<VecDeque<u8>>,
    sample_rate: u32,
    channels: u16,
    frame_size: usize, // bytes per 20ms frame
}

#[pymethods]
impl AudioBuffer {
    #[new]
    #[pyo3(signature = (sample_rate=48000, channels=2))]
    fn new(sample_rate: u32, channels: u16) -> Self {
        // 20ms at given sample rate and channels, 16-bit samples
        let samples_per_frame = sample_rate as usize * 20 / 1000;
        let frame_size = samples_per_frame * channels as usize * 2;
        AudioBuffer {
            buffer: Mutex::new(VecDeque::with_capacity(frame_size * 100)),
            sample_rate,
            channels,
            frame_size,
        }
    }

    /// Push PCM data into the buffer.
    fn push(&self, data: &[u8]) {
        let mut buf = self.buffer.lock().unwrap();
        buf.extend(data);
    }

    /// Read one 20ms frame. Returns None if buffer is empty.
    /// Pads with silence if partial frame available.
    fn read_frame<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
        let mut buf = self.buffer.lock().unwrap();
        if buf.len() >= self.frame_size {
            let frame: Vec<u8> = buf.drain(..self.frame_size).collect();
            Some(PyBytes::new(py, &frame))
        } else if !buf.is_empty() {
            let mut frame: Vec<u8> = buf.drain(..).collect();
            frame.resize(self.frame_size, 0); // pad with silence
            Some(PyBytes::new(py, &frame))
        } else {
            None
        }
    }

    /// True when buffer contains at least one full frame.
    #[getter]
    fn is_playing(&self) -> bool {
        let buf = self.buffer.lock().unwrap();
        buf.len() >= self.frame_size
    }

    /// Estimated remaining audio duration in milliseconds.
    fn duration_remaining_ms(&self) -> f64 {
        let buf = self.buffer.lock().unwrap();
        let bytes_per_ms =
            self.sample_rate as f64 * self.channels as f64 * 2.0 / 1000.0;
        if bytes_per_ms > 0.0 {
            buf.len() as f64 / bytes_per_ms
        } else {
            0.0
        }
    }

    /// Clear the buffer.
    fn clear(&self) {
        let mut buf = self.buffer.lock().unwrap();
        buf.clear();
    }

    /// Current buffer size in bytes.
    fn __len__(&self) -> usize {
        let buf = self.buffer.lock().unwrap();
        buf.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_push_and_duration() {
        Python::with_gil(|py| {
            let buf = AudioBuffer::new(48000, 2);
            // 48kHz stereo 16-bit: 192000 bytes/sec = 192 bytes/ms
            // Push 1920 bytes = 10ms
            let data = vec![0u8; 1920];
            buf.push(&data);
            let dur = buf.duration_remaining_ms();
            assert!((dur - 10.0).abs() < 0.1);

            // Not enough for a full frame (3840 bytes for 20ms)
            assert!(!buf.is_playing());

            // Push another 1920 bytes -> 3840 total = one frame
            buf.push(&data);
            assert!(buf.is_playing());

            // Read frame
            let frame = buf.read_frame(py);
            assert!(frame.is_some());
            assert_eq!(frame.unwrap().as_bytes().len(), 3840);

            // Buffer should be empty now
            assert!(!buf.is_playing());
            assert!(buf.read_frame(py).is_none());
        });
    }

    #[test]
    fn test_partial_frame_padding() {
        Python::with_gil(|py| {
            let buf = AudioBuffer::new(48000, 2);
            // Push less than a frame
            buf.push(&vec![42u8; 100]);
            assert!(!buf.is_playing());

            // read_frame should still return padded frame
            let frame = buf.read_frame(py);
            assert!(frame.is_some());
            let bytes = frame.unwrap();
            assert_eq!(bytes.as_bytes().len(), 3840);
            // First 100 bytes should be 42, rest should be 0
            assert_eq!(bytes.as_bytes()[0], 42);
            assert_eq!(bytes.as_bytes()[99], 42);
            assert_eq!(bytes.as_bytes()[100], 0);
        });
    }

    #[test]
    fn test_clear() {
        let buf = AudioBuffer::new(48000, 2);
        buf.push(&vec![0u8; 10000]);
        assert!(buf.__len__() > 0);
        buf.clear();
        assert_eq!(buf.__len__(), 0);
    }
}
