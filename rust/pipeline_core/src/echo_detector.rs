//! Text-based echo detection.
//!
//! Detects when a transcript is likely the bot's own speech being picked up
//! by other users' microphones. Uses word-overlap ratio against recently
//! spoken text within a configurable time window.

use pyo3::prelude::*;
use std::collections::VecDeque;

struct SpokenEntry {
    words: Vec<String>,
    timestamp: f64,
}

#[pyclass]
pub struct EchoDetector {
    history: VecDeque<SpokenEntry>,
    window_seconds: f64,
}

#[pymethods]
impl EchoDetector {
    #[new]
    #[pyo3(signature = (window_seconds=30.0))]
    fn new(window_seconds: f64) -> Self {
        EchoDetector {
            history: VecDeque::with_capacity(64),
            window_seconds,
        }
    }

    /// Record text that the bot has spoken (for later echo comparison).
    fn record_spoken(&mut self, text: &str) {
        let now = Self::now();
        let words = Self::tokenize(text);
        if !words.is_empty() {
            self.history.push_back(SpokenEntry {
                words,
                timestamp: now,
            });
        }
    }

    /// Check if a transcript is likely an echo of recently spoken text.
    ///
    /// Returns true if the word-overlap ratio with any recent spoken text
    /// exceeds the threshold (default 0.6 = 60% overlap).
    #[pyo3(signature = (transcript, threshold=0.6))]
    fn is_echo(&self, transcript: &str, threshold: f64) -> bool {
        let now = Self::now();
        let transcript_words = Self::tokenize(transcript);
        if transcript_words.is_empty() {
            return false;
        }

        for entry in &self.history {
            if now - entry.timestamp > self.window_seconds {
                continue;
            }
            let overlap = Self::word_overlap_ratio(&transcript_words, &entry.words);
            if overlap >= threshold {
                return true;
            }
        }
        false
    }

    /// Remove expired entries older than the window.
    fn prune(&mut self) {
        let now = Self::now();
        let cutoff = now - self.window_seconds;
        while let Some(front) = self.history.front() {
            if front.timestamp < cutoff {
                self.history.pop_front();
            } else {
                break;
            }
        }
    }

    /// Number of entries currently stored.
    fn __len__(&self) -> usize {
        self.history.len()
    }
}

impl EchoDetector {
    fn now() -> f64 {
        use std::time::{SystemTime, UNIX_EPOCH};
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs_f64()
    }

    /// Tokenize text: lowercase, split on whitespace, strip basic punctuation.
    fn tokenize(text: &str) -> Vec<String> {
        text.to_lowercase()
            .split_whitespace()
            .map(|w| {
                w.trim_matches(|c: char| c.is_ascii_punctuation())
                    .to_string()
            })
            .filter(|w| !w.is_empty())
            .collect()
    }

    /// Compute word overlap ratio: |intersection| / |transcript_words|.
    fn word_overlap_ratio(transcript_words: &[String], spoken_words: &[String]) -> f64 {
        if transcript_words.is_empty() {
            return 0.0;
        }
        let overlap_count = transcript_words
            .iter()
            .filter(|w| spoken_words.contains(w))
            .count();
        overlap_count as f64 / transcript_words.len() as f64
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_exact_echo() {
        Python::with_gil(|_py| {
            let mut detector = EchoDetector::new(30.0);
            detector.record_spoken("Hello, how are you doing today?");
            assert!(detector.is_echo("Hello how are you doing today", 0.6));
        });
    }

    #[test]
    fn test_partial_echo() {
        Python::with_gil(|_py| {
            let mut detector = EchoDetector::new(30.0);
            detector.record_spoken("The quick brown fox jumps over the lazy dog");
            // 5 of 9 words match = 0.56 < 0.6
            assert!(!detector.is_echo("The quick brown fox runs", 0.6));
            // 4 of 5 words match = 0.8 > 0.6
            assert!(detector.is_echo("quick brown fox jumps over", 0.6));
        });
    }

    #[test]
    fn test_no_echo() {
        Python::with_gil(|_py| {
            let mut detector = EchoDetector::new(30.0);
            detector.record_spoken("Hello world");
            assert!(!detector.is_echo("Something completely different", 0.6));
        });
    }

    #[test]
    fn test_empty_transcript() {
        let detector = EchoDetector::new(30.0);
        assert!(!detector.is_echo("", 0.6));
    }

    #[test]
    fn test_prune() {
        let mut detector = EchoDetector::new(0.0); // 0s window = everything expires
        detector.record_spoken("test");
        std::thread::sleep(std::time::Duration::from_millis(10));
        detector.prune();
        assert_eq!(detector.__len__(), 0);
    }
}
