"""Deterministic constellation fingerprints for exact and near-exact audio reuse."""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

import numpy as np
from scipy.ndimage import maximum_filter
from scipy.signal import stft

from auxris.core.models import Fingerprint


@dataclass(frozen=True, slots=True)
class FingerprintConfig:
    sample_rate: int = 11_025
    n_fft: int = 2_048
    hop_length: int = 512
    peak_frequency_radius: int = 8
    peak_time_radius: int = 2
    peaks_per_frame: int = 5
    fanout: int = 8
    min_pair_delta: int = 1
    max_pair_delta: int = 60
    minimum_db: float = -55.0

    @property
    def frame_seconds(self) -> float:
        return self.hop_length / self.sample_rate


class Fingerprinter:
    """Create versioned landmark-pair fingerprints from normalized mono audio."""

    algorithm = "auxris.constellation.v1"

    def __init__(self, config: FingerprintConfig | None = None) -> None:
        self.config = config or FingerprintConfig()

    def fingerprint(self, samples: np.ndarray) -> list[Fingerprint]:
        config = self.config
        if samples.ndim != 1:
            raise ValueError("Expected mono audio")
        if len(samples) < config.n_fft:
            return []
        if float(np.sqrt(np.mean(np.square(samples), dtype=np.float64))) < 1e-6:
            return []

        _, _, spectrum = stft(
            samples,
            fs=config.sample_rate,
            window="hann",
            nperseg=config.n_fft,
            noverlap=config.n_fft - config.hop_length,
            nfft=config.n_fft,
            boundary=None,
            padded=False,
        )
        magnitude = np.abs(spectrum)
        reference = max(float(magnitude.max()), np.finfo(np.float32).tiny)
        db = 20.0 * np.log10(np.maximum(magnitude, np.finfo(np.float32).tiny) / reference)

        local_max = maximum_filter(
            db,
            size=(2 * config.peak_frequency_radius + 1, 2 * config.peak_time_radius + 1),
            mode="nearest",
        )
        mask = (db == local_max) & (db >= config.minimum_db)

        peaks: list[tuple[int, int]] = []
        for frame in range(mask.shape[1]):
            bins = np.flatnonzero(mask[:, frame])
            if len(bins) == 0:
                continue
            strongest = bins[np.argsort(db[bins, frame])[-config.peaks_per_frame :]]
            peaks.extend((frame, int(bin_index)) for bin_index in sorted(strongest))

        fingerprints: list[Fingerprint] = []
        for anchor_index, (anchor_frame, anchor_bin) in enumerate(peaks):
            paired = 0
            for target_frame, target_bin in peaks[anchor_index + 1 :]:
                delta = target_frame - anchor_frame
                if delta < config.min_pair_delta:
                    continue
                if delta > config.max_pair_delta:
                    break
                payload = struct.pack(">HHH", anchor_bin, target_bin, delta)
                digest = hashlib.blake2b(
                    payload,
                    digest_size=8,
                    person=b"auxrisv1",
                ).digest()
                fingerprints.append(Fingerprint(hash_bytes=digest, frame=anchor_frame))
                paired += 1
                if paired >= config.fanout:
                    break

        return fingerprints
