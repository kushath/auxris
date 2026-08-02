from __future__ import annotations

import numpy as np

from auxris.core.fingerprint import FingerprintConfig, Fingerprinter
from tests.conftest import synthetic_audio


def test_fingerprint_is_deterministic() -> None:
    audio = synthetic_audio(duration=5)
    engine = Fingerprinter()
    first = engine.fingerprint(audio)
    second = engine.fingerprint(audio.copy())
    assert first
    assert first == second
    assert all(len(item.hash_bytes) == 8 for item in first)


def test_silence_produces_no_fingerprints() -> None:
    config = FingerprintConfig()
    audio = np.zeros(config.sample_rate * 3, dtype=np.float32)
    assert Fingerprinter(config).fingerprint(audio) == []
