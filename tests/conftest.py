from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import chirp


def synthetic_audio(
    *,
    sample_rate: int = 11_025,
    duration: float = 12.0,
    seed: int = 7,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    time = np.arange(int(sample_rate * duration), dtype=np.float64) / sample_rate
    signal = np.zeros_like(time)
    for segment in range(int(duration)):
        start = segment * sample_rate
        stop = min((segment + 1) * sample_rate, len(time))
        local_time = np.arange(stop - start, dtype=np.float64) / sample_rate
        base = 170 + ((segment * 137 + seed * 29) % 900)
        signal[start:stop] = (
            0.30 * chirp(local_time, f0=base, f1=base * 2.4, t1=1.0, method="linear")
            + 0.18 * np.sin(2 * np.pi * (base * 1.51) * local_time)
            + 0.09 * np.sin(2 * np.pi * (base * 2.07) * local_time)
        )
    signal += rng.normal(0, 0.002, len(signal))
    return signal.astype(np.float32)


def write_audio(path: Path, samples: np.ndarray, sample_rate: int = 11_025) -> Path:
    sf.write(path, samples, sample_rate, subtype="PCM_16")
    return path
