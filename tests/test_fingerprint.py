from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

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


def test_shared_landmark_hash_vectors() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "constellation-v1.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["protocol"] == Fingerprinter.algorithm

    for case in fixture["normative_cases"]:
        payload = struct.pack(
            ">HHH",
            case["anchor_bin"],
            case["target_bin"],
            case["delta_frames"],
        )
        assert payload.hex() == case["payload_hex"]
        assert (
            hashlib.blake2b(payload, digest_size=8, person=b"auxrisv1").hexdigest()
            == case["hash_hex"]
        )


def test_shared_signal_vector() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "constellation-v1.json"
    signal_case = json.loads(fixture_path.read_text(encoding="utf-8"))["signal_case"]
    state = signal_case["seed"]
    values = []
    for _ in range(signal_case["sample_count"]):
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        values.append(np.float32(((state >> 16) - 32_768) / 32_768.0))

    fingerprints = Fingerprinter().fingerprint(np.asarray(values, dtype=np.float32))
    assert len(fingerprints) == signal_case["fingerprint_count"]
    stream = b"".join(item.hash_bytes + item.frame.to_bytes(8, "big") for item in fingerprints)
    assert (
        hashlib.blake2b(stream, digest_size=32, person=b"auxrisvec").hexdigest()
        == signal_case["fingerprint_stream_blake2b_256"]
    )
