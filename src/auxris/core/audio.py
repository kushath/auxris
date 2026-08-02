"""Bounded audio decoding and normalization."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly


class AudioDecodeError(ValueError):
    """Raised when input cannot be safely decoded as supported audio."""


@dataclass(frozen=True, slots=True)
class DecodedAudio:
    samples: np.ndarray
    sample_rate: int
    duration_seconds: float
    sha256: str


def _decode(source: str | Path | BinaryIO, target_sample_rate: int) -> DecodedAudio:
    try:
        samples, sample_rate = sf.read(source, dtype="float32", always_2d=True)
    except (RuntimeError, TypeError, ValueError) as exc:
        raise AudioDecodeError(f"Unsupported or malformed audio: {exc}") from exc

    if sample_rate <= 0 or samples.size == 0:
        raise AudioDecodeError("Audio contains no decodable samples")
    if not np.isfinite(samples).all():
        raise AudioDecodeError("Audio contains non-finite samples")

    mono = samples.mean(axis=1, dtype=np.float32)
    if sample_rate != target_sample_rate:
        divisor = int(np.gcd(sample_rate, target_sample_rate))
        mono = resample_poly(
            mono,
            target_sample_rate // divisor,
            sample_rate // divisor,
        ).astype(np.float32, copy=False)

    peak = float(np.max(np.abs(mono)))
    if peak > 1.0:
        mono = mono / peak

    canonical = mono.astype("<f4", copy=False).tobytes()
    return DecodedAudio(
        samples=mono,
        sample_rate=target_sample_rate,
        duration_seconds=len(mono) / target_sample_rate,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def decode_path(path: str | Path, target_sample_rate: int) -> DecodedAudio:
    """Decode a local audio file supported by libsndfile."""

    return _decode(Path(path), target_sample_rate)


def decode_bytes(data: bytes, target_sample_rate: int) -> DecodedAudio:
    """Decode bounded in-memory data supplied by the API layer."""

    return _decode(io.BytesIO(data), target_sample_rate)
