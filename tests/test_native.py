from __future__ import annotations

import numpy as np
import pytest

from auxris.core.fingerprint import Fingerprinter
from auxris.core.native import NativeStreamingFingerprinter, fingerprint_native
from tests.conftest import synthetic_audio

pytest.importorskip("auxris_native")


def test_native_batch_matches_python_protocol() -> None:
    audio = synthetic_audio(duration=3, seed=41)
    assert fingerprint_native(audio) == Fingerprinter().fingerprint(audio)


def test_python_adapter_canonicalizes_non_contiguous_arrays() -> None:
    audio = synthetic_audio(duration=5, seed=47)[::2]
    assert not audio.flags.c_contiguous
    assert fingerprint_native(audio) == Fingerprinter().fingerprint(audio)


@pytest.mark.parametrize("chunk_size", [1, 137, 512, 4_096])
def test_native_stream_is_chunk_invariant(chunk_size: int) -> None:
    audio = synthetic_audio(duration=3, seed=43)
    expected = Fingerprinter().fingerprint(audio)
    stream = NativeStreamingFingerprinter(max_samples=len(audio))
    for chunk in (audio[start : start + chunk_size] for start in range(0, len(audio), chunk_size)):
        stream.push(chunk)
        assert stream.buffered_sample_count < 2_048
    assert stream.finish() == expected


def test_native_rejects_non_finite_pcm() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        fingerprint_native(np.full(2_048, np.nan, dtype=np.float32))
