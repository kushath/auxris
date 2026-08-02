"""Optional adapter for the separately distributed Rust extension."""

from __future__ import annotations

import importlib
from functools import lru_cache
from types import ModuleType
from typing import Protocol, cast

import numpy as np

from auxris.core.models import Fingerprint


class NativeBackendUnavailable(RuntimeError):
    """Raised when the optional ``auxris-native`` package is not installed."""


class _NativeStream(Protocol):
    def push(self, samples: np.ndarray) -> None: ...

    def finish(self) -> list[tuple[str, int]]: ...

    def buffered_sample_count(self) -> int: ...

    def retained_candidate_count(self) -> int: ...


class _NativeStreamFactory(Protocol):
    def __call__(self, max_samples: int | None = None) -> _NativeStream: ...


class _NativeModule(Protocol):
    ALGORITHM: str
    StreamingFingerprinter: _NativeStreamFactory

    def fingerprint_hex(
        self,
        samples: np.ndarray,
        max_samples: int | None = None,
    ) -> list[tuple[str, int]]: ...


@lru_cache(maxsize=1)
def _load() -> _NativeModule:
    try:
        module: ModuleType = importlib.import_module("auxris_native")
    except ImportError as exc:
        raise NativeBackendUnavailable(
            "Install the separately versioned 'auxris-native' wheel to use the Rust backend"
        ) from exc
    return cast(_NativeModule, module)


def native_available() -> bool:
    """Return whether the optional extension can be imported."""

    try:
        _load()
    except NativeBackendUnavailable:
        return False
    return True


def fingerprint_native(
    samples: np.ndarray,
    *,
    max_samples: int | None = None,
) -> list[Fingerprint]:
    """Fingerprint mono float32 PCM with the optional Rust kernel."""

    normalized = _canonical_samples(samples)
    encoded = _load().fingerprint_hex(normalized, max_samples)
    return [
        Fingerprint(hash_bytes=bytes.fromhex(hash_hex), frame=frame) for hash_hex, frame in encoded
    ]


class NativeStreamingFingerprinter:
    """Python-owned facade over the Rust rolling extractor."""

    def __init__(self, *, max_samples: int | None = None) -> None:
        self._inner = _load().StreamingFingerprinter(max_samples)

    def push(self, samples: np.ndarray) -> None:
        """Ingest one mono float32 chunk without retaining the full clip."""

        self._inner.push(_canonical_samples(samples))

    def finish(self) -> list[Fingerprint]:
        """Finalize the subject and return protocol-compatible fingerprints."""

        return [
            Fingerprint(hash_bytes=bytes.fromhex(hash_hex), frame=frame)
            for hash_hex, frame in self._inner.finish()
        ]

    @property
    def buffered_sample_count(self) -> int:
        return self._inner.buffered_sample_count()

    @property
    def retained_candidate_count(self) -> int:
        return self._inner.retained_candidate_count()


def _canonical_samples(samples: np.ndarray) -> np.ndarray:
    if samples.ndim != 1:
        raise ValueError("Expected mono audio")
    return np.ascontiguousarray(samples, dtype=np.float32)
