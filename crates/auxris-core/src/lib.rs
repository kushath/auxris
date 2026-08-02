//! Deterministic, resource-bounded primitives for Auxris audio evidence.
//!
//! This crate implements the `auxris.constellation.v1` protocol defined in
//! RFC-0002. It deliberately contains no decoder, network client, policy
//! engine, or legal interpretation.

use std::error::Error;
use std::f32::consts::PI;
use std::fmt;

use blake2b_simd::Params;
use rustfft::num_complex::Complex32;
use rustfft::FftPlanner;

mod stream;

pub use stream::StreamingFingerprinter;

/// Stable identifier for this fingerprint protocol.
pub const ALGORITHM: &str = "auxris.constellation.v1";
const PERSONALIZATION: &[u8; 8] = b"auxrisv1";

/// Protocol and resource-limit configuration.
#[derive(Clone, Debug)]
pub struct FingerprintConfig {
    /// Canonical input sample rate.
    pub sample_rate: usize,
    /// FFT frame length.
    pub n_fft: usize,
    /// Samples advanced between frames.
    pub hop_length: usize,
    /// Frequency-bin radius for local maxima.
    pub peak_frequency_radius: usize,
    /// Frame radius for local maxima.
    pub peak_time_radius: usize,
    /// Maximum retained peaks per frame.
    pub peaks_per_frame: usize,
    /// Maximum target peaks paired with an anchor.
    pub fanout: usize,
    /// Minimum pair separation in frames.
    pub min_pair_delta: usize,
    /// Maximum pair separation in frames.
    pub max_pair_delta: usize,
    /// Relative dB floor for peaks.
    pub minimum_db: f32,
    /// Maximum samples accepted by the bounded-batch adapter.
    pub max_samples: usize,
    /// Maximum fingerprints emitted for one clip.
    pub max_fingerprints: usize,
}

impl Default for FingerprintConfig {
    fn default() -> Self {
        Self {
            sample_rate: 11_025,
            n_fft: 2_048,
            hop_length: 512,
            peak_frequency_radius: 8,
            peak_time_radius: 2,
            peaks_per_frame: 5,
            fanout: 8,
            min_pair_delta: 1,
            max_pair_delta: 60,
            minimum_db: -55.0,
            max_samples: 11_025 * 60 * 10,
            max_fingerprints: 2_000_000,
        }
    }
}

impl FingerprintConfig {
    /// Validate arithmetic and protocol bounds before processing untrusted data.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::InvalidConfig`] when a value is unsafe or
    /// incompatible with the v1 wire representation.
    pub fn validate(&self) -> Result<(), FingerprintError> {
        if self.sample_rate == 0
            || self.n_fft < 2
            || !self.n_fft.is_power_of_two()
            || self.hop_length == 0
            || self.hop_length > self.n_fft
            || self.peaks_per_frame == 0
            || self.fanout == 0
            || self.min_pair_delta == 0
            || self.max_pair_delta < self.min_pair_delta
            || self.max_samples < self.n_fft
            || self.max_fingerprints == 0
            || !self.minimum_db.is_finite()
            || self.n_fft / 2 > usize::from(u16::MAX)
            || self.max_pair_delta > usize::from(u16::MAX)
        {
            return Err(FingerprintError::InvalidConfig);
        }
        Ok(())
    }

    /// Duration represented by one anchor-frame step.
    #[must_use]
    #[allow(clippy::cast_precision_loss)]
    pub fn frame_seconds(&self) -> f64 {
        self.hop_length as f64 / self.sample_rate as f64
    }
}

/// A landmark-pair retrieval key and its anchor frame.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Fingerprint {
    /// Personalized 64-bit `BLAKE2b` retrieval key.
    pub hash_bytes: [u8; 8],
    /// Zero-based anchor frame.
    pub frame: u64,
}

/// Failures caused by invalid configuration or bounded, untrusted input.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum FingerprintError {
    /// Configuration violates protocol or arithmetic requirements.
    InvalidConfig,
    /// Input contains NaN or infinity.
    NonFiniteSample,
    /// Input exceeds the configured sample limit.
    SampleLimitExceeded {
        /// Configured maximum number of samples.
        limit: usize,
    },
    /// Output would exceed the configured fingerprint limit.
    FingerprintLimitExceeded {
        /// Configured maximum number of emitted fingerprints.
        limit: usize,
    },
}

impl fmt::Display for FingerprintError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidConfig => formatter.write_str("invalid fingerprint configuration"),
            Self::NonFiniteSample => formatter.write_str("input contains a non-finite sample"),
            Self::SampleLimitExceeded { limit } => {
                write!(formatter, "input exceeds the {limit}-sample limit")
            }
            Self::FingerprintLimitExceeded { limit } => {
                write!(formatter, "output exceeds the {limit}-fingerprint limit")
            }
        }
    }
}

impl Error for FingerprintError {}

/// Serialize and hash one landmark pair exactly as RFC-0002 specifies.
#[must_use]
pub fn hash_landmark(anchor_bin: u16, target_bin: u16, delta_frames: u16) -> [u8; 8] {
    let mut payload = [0_u8; 6];
    payload[0..2].copy_from_slice(&anchor_bin.to_be_bytes());
    payload[2..4].copy_from_slice(&target_bin.to_be_bytes());
    payload[4..6].copy_from_slice(&delta_frames.to_be_bytes());

    let digest = Params::new()
        .hash_length(8)
        .personal(PERSONALIZATION)
        .hash(&payload);
    let mut result = [0_u8; 8];
    result.copy_from_slice(digest.as_bytes());
    result
}

/// Fingerprint canonical mono float32 PCM in bounded-batch mode.
///
/// # Errors
///
/// Returns [`FingerprintError`] for invalid configuration, non-finite or
/// oversized input, or output that reaches its configured resource limit.
pub fn fingerprint_samples(
    samples: &[f32],
    config: &FingerprintConfig,
) -> Result<Vec<Fingerprint>, FingerprintError> {
    config.validate()?;
    if samples.len() > config.max_samples {
        return Err(FingerprintError::SampleLimitExceeded {
            limit: config.max_samples,
        });
    }
    if samples.iter().any(|sample| !sample.is_finite()) {
        return Err(FingerprintError::NonFiniteSample);
    }
    if samples.len() < config.n_fft || is_silent(samples) {
        return Ok(Vec::new());
    }

    let frame_count = 1 + (samples.len() - config.n_fft) / config.hop_length;
    let bin_count = config.n_fft / 2 + 1;
    let window = periodic_hann(config.n_fft);
    let mut planner = FftPlanner::<f32>::new();
    let fft = planner.plan_fft_forward(config.n_fft);
    let mut magnitudes = Vec::with_capacity(frame_count);
    let mut reference = f32::MIN_POSITIVE;

    for frame in 0..frame_count {
        let start = frame * config.hop_length;
        let mut buffer: Vec<Complex32> = samples[start..start + config.n_fft]
            .iter()
            .zip(&window)
            .map(|(sample, weight)| Complex32::new(sample * weight, 0.0))
            .collect();
        fft.process(&mut buffer);
        let frame_magnitudes: Vec<f32> = buffer[..bin_count]
            .iter()
            .map(|value| value.norm())
            .collect();
        reference = frame_magnitudes.iter().copied().fold(reference, f32::max);
        magnitudes.push(frame_magnitudes);
    }

    let db: Vec<Vec<f32>> = magnitudes
        .into_iter()
        .map(|frame| {
            frame
                .into_iter()
                .map(|magnitude| 20.0 * (magnitude.max(f32::MIN_POSITIVE) / reference).log10())
                .collect()
        })
        .collect();

    let peaks = find_peaks(&db, config);
    pair_peaks(&peaks, config)
}

/// Compatibility wrapper around the rolling extractor.
#[derive(Debug)]
pub struct BoundedBatchFingerprinter {
    inner: StreamingFingerprinter,
}

impl BoundedBatchFingerprinter {
    /// Create an empty accumulator after validating configuration.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::InvalidConfig`] for unsafe configuration.
    pub fn new(config: FingerprintConfig) -> Result<Self, FingerprintError> {
        Ok(Self {
            inner: StreamingFingerprinter::new(config)?,
        })
    }

    /// Add canonical samples, enforcing the configured limit before allocation.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::SampleLimitExceeded`] or
    /// [`FingerprintError::NonFiniteSample`] when the chunk is not acceptable.
    pub fn push(&mut self, chunk: &[f32]) -> Result<(), FingerprintError> {
        self.inner.push(chunk)
    }

    /// Consume the accumulator and emit deterministic fingerprints.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError`] when final processing violates an input or
    /// output bound.
    pub fn finish(self) -> Result<Vec<Fingerprint>, FingerprintError> {
        self.inner.finish()
    }
}

#[allow(clippy::cast_precision_loss)]
pub(crate) fn periodic_hann(length: usize) -> Vec<f32> {
    (0..length)
        .map(|index| 0.5 - 0.5 * (2.0 * PI * index as f32 / length as f32).cos())
        .collect()
}

#[allow(clippy::cast_precision_loss)]
fn is_silent(samples: &[f32]) -> bool {
    let mean_square = samples
        .iter()
        .map(|sample| f64::from(*sample) * f64::from(*sample))
        .sum::<f64>()
        / samples.len() as f64;
    mean_square.sqrt() < 1e-6
}

fn find_peaks(db: &[Vec<f32>], config: &FingerprintConfig) -> Vec<(usize, u16)> {
    let frame_count = db.len();
    let bin_count = db[0].len();
    let mut peaks = Vec::new();

    for frame in 0..frame_count {
        let frame_start = frame.saturating_sub(config.peak_time_radius);
        let frame_end = frame
            .saturating_add(config.peak_time_radius)
            .min(frame_count - 1);
        let mut candidates = Vec::new();

        for bin in 0..bin_count {
            let value = db[frame][bin];
            if value < config.minimum_db {
                continue;
            }
            let bin_start = bin.saturating_sub(config.peak_frequency_radius);
            let bin_end = bin
                .saturating_add(config.peak_frequency_radius)
                .min(bin_count - 1);
            let is_local_maximum = (frame_start..=frame_end).all(|near_frame| {
                (bin_start..=bin_end).all(|near_bin| db[near_frame][near_bin] <= value)
            });
            if is_local_maximum {
                candidates.push((value, bin));
            }
        }

        candidates.sort_by(|left, right| {
            left.0
                .total_cmp(&right.0)
                .then_with(|| left.1.cmp(&right.1))
        });
        let keep_from = candidates.len().saturating_sub(config.peaks_per_frame);
        let mut retained: Vec<usize> = candidates[keep_from..]
            .iter()
            .map(|(_, bin)| *bin)
            .collect();
        retained.sort_unstable();
        peaks.extend(retained.into_iter().map(|bin| {
            (
                frame,
                u16::try_from(bin).expect("configuration validates frequency-bin range"),
            )
        }));
    }
    peaks
}

pub(crate) fn pair_peaks(
    peaks: &[(usize, u16)],
    config: &FingerprintConfig,
) -> Result<Vec<Fingerprint>, FingerprintError> {
    let mut fingerprints = Vec::new();
    for (anchor_index, &(anchor_frame, anchor_bin)) in peaks.iter().enumerate() {
        let mut paired = 0;
        for &(target_frame, target_bin) in &peaks[anchor_index + 1..] {
            let delta = target_frame - anchor_frame;
            if delta < config.min_pair_delta {
                continue;
            }
            if delta > config.max_pair_delta {
                break;
            }
            if fingerprints.len() == config.max_fingerprints {
                return Err(FingerprintError::FingerprintLimitExceeded {
                    limit: config.max_fingerprints,
                });
            }
            fingerprints.push(Fingerprint {
                hash_bytes: hash_landmark(
                    anchor_bin,
                    target_bin,
                    u16::try_from(delta).expect("configuration validates frame delta range"),
                ),
                frame: u64::try_from(anchor_frame).expect("frame index fits into u64"),
            });
            paired += 1;
            if paired == config.fanout {
                break;
            }
        }
    }
    Ok(fingerprints)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn silence_is_empty() {
        let config = FingerprintConfig::default();
        let samples = vec![0.0; config.n_fft * 2];
        assert_eq!(fingerprint_samples(&samples, &config), Ok(Vec::new()));
    }

    #[test]
    #[allow(clippy::cast_precision_loss)]
    fn rolling_chunks_match_the_batch_reference() {
        let config = FingerprintConfig::default();
        let samples: Vec<f32> = (0..config.sample_rate * 2)
            .map(|index| (2.0 * PI * 440.0 * index as f32 / config.sample_rate as f32).sin())
            .collect();
        let direct = fingerprint_samples(&samples, &config).expect("direct fingerprint");
        for chunk_size in [1, 31, 512, 733, 4_096] {
            let mut chunked = StreamingFingerprinter::new(config.clone()).expect("valid config");
            for chunk in samples.chunks(chunk_size) {
                chunked.push(chunk).expect("bounded input");
                assert!(chunked.buffered_sample_count() < config.n_fft);
                assert!(chunked.buffered_spectrum_count() <= 2 * config.peak_time_radius + 1);
            }
            assert_eq!(chunked.finish().expect("chunked fingerprint"), direct);
        }
    }

    #[test]
    fn rejects_non_finite_input() {
        let config = FingerprintConfig::default();
        assert_eq!(
            fingerprint_samples(&[f32::NAN; 2_048], &config),
            Err(FingerprintError::NonFiniteSample)
        );
    }

    #[test]
    fn enforces_sample_limit_before_append() {
        let config = FingerprintConfig {
            max_samples: 2_048,
            ..FingerprintConfig::default()
        };
        let mut engine = BoundedBatchFingerprinter::new(config).expect("valid config");
        assert_eq!(
            engine.push(&[0.0; 2_049]),
            Err(FingerprintError::SampleLimitExceeded { limit: 2_048 })
        );
    }
}
