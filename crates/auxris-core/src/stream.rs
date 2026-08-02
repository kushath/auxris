//! Rolling constellation extraction with chunk-invariant output.

use std::collections::VecDeque;
use std::fmt;
use std::sync::Arc;

use rustfft::num_complex::Complex32;
use rustfft::{Fft, FftPlanner};

use crate::{pair_peaks, periodic_hann, Fingerprint, FingerprintConfig, FingerprintError};

#[derive(Debug)]
struct SpectrumFrame {
    index: usize,
    magnitudes: Vec<f32>,
}

#[derive(Debug)]
struct CandidatePeak {
    frame: usize,
    bin: u16,
    magnitude: f32,
}

/// Incrementally extracts v1 fingerprints without retaining the full PCM clip.
///
/// The extractor keeps only the overlapping PCM tail, the local-maximum time
/// neighborhood, and at most `peaks_per_frame` candidates for each processed
/// frame. Final hashes are emitted by [`Self::finish`] because v1's dB floor is
/// relative to the largest magnitude in the complete subject.
pub struct StreamingFingerprinter {
    config: FingerprintConfig,
    window: Vec<f32>,
    fft: Arc<dyn Fft<f32>>,
    pcm: Vec<f32>,
    pcm_start: usize,
    next_frame_start: usize,
    spectra: VecDeque<SpectrumFrame>,
    next_peak_frame: usize,
    candidates: Vec<CandidatePeak>,
    reference: f32,
    sample_count: usize,
    sum_squares: f64,
}

impl fmt::Debug for StreamingFingerprinter {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("StreamingFingerprinter")
            .field("config", &self.config)
            .field("buffered_samples", &self.pcm.len())
            .field("buffered_spectra", &self.spectra.len())
            .field("candidate_peaks", &self.candidates.len())
            .field("sample_count", &self.sample_count)
            .finish_non_exhaustive()
    }
}

impl StreamingFingerprinter {
    /// Create an empty rolling extractor.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::InvalidConfig`] when configuration is unsafe
    /// or incompatible with the v1 protocol.
    pub fn new(config: FingerprintConfig) -> Result<Self, FingerprintError> {
        config.validate()?;
        let mut planner = FftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(config.n_fft);
        let window = periodic_hann(config.n_fft);
        Ok(Self {
            config,
            window,
            fft,
            pcm: Vec::new(),
            pcm_start: 0,
            next_frame_start: 0,
            spectra: VecDeque::new(),
            next_peak_frame: 0,
            candidates: Vec::new(),
            reference: f32::MIN_POSITIVE,
            sample_count: 0,
            sum_squares: 0.0,
        })
    }

    /// Ingest one canonical mono float32 PCM chunk.
    ///
    /// Empty chunks are accepted. A rejected chunk does not partially mutate
    /// the extractor.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::SampleLimitExceeded`] or
    /// [`FingerprintError::NonFiniteSample`] for unacceptable input.
    pub fn push(&mut self, chunk: &[f32]) -> Result<(), FingerprintError> {
        let next_count = self.sample_count.checked_add(chunk.len()).ok_or(
            FingerprintError::SampleLimitExceeded {
                limit: self.config.max_samples,
            },
        )?;
        if next_count > self.config.max_samples {
            return Err(FingerprintError::SampleLimitExceeded {
                limit: self.config.max_samples,
            });
        }
        if chunk.iter().any(|sample| !sample.is_finite()) {
            return Err(FingerprintError::NonFiniteSample);
        }

        for sample in chunk {
            self.sum_squares += f64::from(*sample) * f64::from(*sample);
        }
        self.sample_count = next_count;
        self.pcm.extend_from_slice(chunk);
        self.process_available_frames();
        Ok(())
    }

    /// Finish local-maximum extraction, apply the global dB floor, and hash.
    ///
    /// # Errors
    ///
    /// Returns [`FingerprintError::FingerprintLimitExceeded`] if the configured
    /// output bound is reached.
    pub fn finish(mut self) -> Result<Vec<Fingerprint>, FingerprintError> {
        self.process_remaining_peaks();
        if self.sample_count < self.config.n_fft || self.is_silent() {
            return Ok(Vec::new());
        }

        let threshold = self.reference * 10.0_f32.powf(self.config.minimum_db / 20.0);
        let peaks: Vec<(usize, u16)> = self
            .candidates
            .into_iter()
            .filter(|candidate| candidate.magnitude >= threshold)
            .map(|candidate| (candidate.frame, candidate.bin))
            .collect();
        pair_peaks(&peaks, &self.config)
    }

    /// Current PCM tail retained to bridge the next STFT frame boundary.
    #[must_use]
    pub fn buffered_sample_count(&self) -> usize {
        self.pcm.len()
    }

    /// Current spectrogram frames retained for the local time neighborhood.
    #[must_use]
    pub fn buffered_spectrum_count(&self) -> usize {
        self.spectra.len()
    }

    /// Candidate peaks retained until the final relative dB threshold is known.
    #[must_use]
    pub fn retained_candidate_count(&self) -> usize {
        self.candidates.len()
    }

    fn process_available_frames(&mut self) {
        let available_end = self.pcm_start + self.pcm.len();
        while self
            .next_frame_start
            .checked_add(self.config.n_fft)
            .is_some_and(|frame_end| frame_end <= available_end)
        {
            let local_start = self.next_frame_start - self.pcm_start;
            let frame_index = self.next_frame_start / self.config.hop_length;
            let mut buffer: Vec<Complex32> = self.pcm[local_start..local_start + self.config.n_fft]
                .iter()
                .zip(&self.window)
                .map(|(sample, weight)| Complex32::new(sample * weight, 0.0))
                .collect();
            self.fft.process(&mut buffer);
            let bin_count = self.config.n_fft / 2 + 1;
            let magnitudes: Vec<f32> = buffer[..bin_count]
                .iter()
                .map(|value| value.norm())
                .collect();
            self.reference = magnitudes.iter().copied().fold(self.reference, f32::max);
            self.next_frame_start += self.config.hop_length;
            self.spectra.push_back(SpectrumFrame {
                index: frame_index,
                magnitudes,
            });
            self.process_ready_peaks();
        }

        let discard = self.next_frame_start - self.pcm_start;
        if discard > 0 {
            self.pcm.drain(..discard);
            self.pcm_start = self.next_frame_start;
        }
    }

    fn process_ready_peaks(&mut self) {
        let Some(latest_frame) = self.spectra.back().map(|frame| frame.index) else {
            return;
        };
        while self
            .next_peak_frame
            .checked_add(self.config.peak_time_radius)
            .is_some_and(|required| required <= latest_frame)
        {
            self.extract_peak_candidates(self.next_peak_frame);
            self.next_peak_frame += 1;
            self.discard_old_spectra();
        }
    }

    fn process_remaining_peaks(&mut self) {
        let Some(last_frame) = self.spectra.back().map(|frame| frame.index) else {
            return;
        };
        while self.next_peak_frame <= last_frame {
            self.extract_peak_candidates(self.next_peak_frame);
            self.next_peak_frame += 1;
            self.discard_old_spectra();
        }
    }

    fn extract_peak_candidates(&mut self, target_frame: usize) {
        let Some(target) = self
            .spectra
            .iter()
            .find(|frame| frame.index == target_frame)
        else {
            return;
        };
        let frame_start = target_frame.saturating_sub(self.config.peak_time_radius);
        let frame_end = target_frame.saturating_add(self.config.peak_time_radius);
        let bin_count = target.magnitudes.len();
        let mut candidates = Vec::new();

        for bin in 0..bin_count {
            let value = target.magnitudes[bin];
            let bin_start = bin.saturating_sub(self.config.peak_frequency_radius);
            let bin_end = bin
                .saturating_add(self.config.peak_frequency_radius)
                .min(bin_count - 1);
            let is_local_maximum = self
                .spectra
                .iter()
                .filter(|frame| (frame_start..=frame_end).contains(&frame.index))
                .all(|frame| {
                    frame.magnitudes[bin_start..=bin_end]
                        .iter()
                        .all(|nearby| *nearby <= value)
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
        let keep_from = candidates.len().saturating_sub(self.config.peaks_per_frame);
        let mut retained = candidates.split_off(keep_from);
        retained.sort_by_key(|(_, bin)| *bin);
        self.candidates
            .extend(retained.into_iter().map(|(magnitude, bin)| CandidatePeak {
                frame: target_frame,
                bin: u16::try_from(bin).expect("configuration validates frequency-bin range"),
                magnitude,
            }));
    }

    fn discard_old_spectra(&mut self) {
        let keep_from = self
            .next_peak_frame
            .saturating_sub(self.config.peak_time_radius);
        while self
            .spectra
            .front()
            .is_some_and(|frame| frame.index < keep_from)
        {
            self.spectra.pop_front();
        }
    }

    #[allow(clippy::cast_precision_loss)]
    fn is_silent(&self) -> bool {
        if self.sample_count == 0 {
            return true;
        }
        (self.sum_squares / self.sample_count as f64).sqrt() < 1e-6
    }
}
