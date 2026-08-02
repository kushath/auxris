"""License-safe transformation benchmark for the constellation reference engine."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np
from scipy import __version__ as scipy_version
from scipy.signal import butter, resample_poly, sosfilt

from auxris import __version__
from auxris.core.audio import DecodedAudio
from auxris.core.fingerprint import FingerprintConfig
from auxris.core.index import Catalog
from auxris.core.models import Asset

SAMPLE_RATE = 11_025
REPORT_SCHEMA = "auxris.benchmark.v1"
Transform = Callable[[np.ndarray, np.random.Generator], np.ndarray]


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One benchmark query and its expected/retrieved catalog identity."""

    case_id: str
    transformation: str
    expected_asset_id: str | None
    matched_asset_id: str | None
    correct: bool
    query_seconds: float
    query_sha256: str
    latency_ms: float
    fingerprints: int
    aligned_votes: int | None
    coverage: float | None


def synthetic_recording(*, duration: float, seed: int) -> np.ndarray:
    """Generate deterministic, redistributable audio without source media."""

    sample_count = int(SAMPLE_RATE * duration)
    signal = np.empty(sample_count, dtype=np.float32)
    state = seed & 0xFFFFFFFF
    for index in range(sample_count):
        segment = index // SAMPLE_RATE
        first_period = 18 + ((seed * 17 + segment * 23) % 83)
        second_period = 11 + ((seed * 31 + segment * 37) % 53)
        first = _triangle_q15(index, first_period)
        second = _triangle_q15(index, second_period)
        state = (1_664_525 * state + 1_013_904_223) & 0xFFFFFFFF
        noise = ((state >> 24) - 128) * 4
        fixed_point = (3 * first + 2 * second) // 10 + noise
        signal[index] = np.float32(fixed_point / 32_768.0)
    return signal


def _triangle_q15(index: int, period: int) -> int:
    phase = (index % period) * 65_536 // period
    return 2 * abs(phase - 32_768) - 32_768


def run_transformation_benchmark(
    *,
    source_count: int = 4,
    source_duration: float = 8.0,
    query_duration: float = 4.0,
    seed: int = 2026,
    minimum_votes: int = 8,
    minimum_coverage: float = 0.015,
) -> dict[str, Any]:
    """Run a deterministic synthetic retrieval benchmark and return a report."""

    if source_count < 1 or source_count > 100:
        raise ValueError("source_count must be between 1 and 100")
    if query_duration < 1 or source_duration < query_duration + 1:
        raise ValueError("source_duration must exceed query_duration by at least one second")
    if query_duration > 600 or source_duration > 3_600:
        raise ValueError("query_duration is capped at 600s and source_duration at 3600s")
    if minimum_votes < 1 or not 0 < minimum_coverage <= 1:
        raise ValueError("thresholds must be positive and coverage cannot exceed one")

    config = FingerprintConfig()
    transformations = _transformations()
    digest = hashlib.sha256(b"auxris.synthetic-transformations.v1\0")
    cases: list[CaseResult] = []
    started = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="auxris-benchmark-") as temp_directory:
        catalog = Catalog(Path(temp_directory) / "catalog.sqlite3", config=config)
        sources: list[tuple[str, np.ndarray]] = []
        for index in range(source_count):
            asset_id = f"synthetic:source:{index:03d}"
            samples = synthetic_recording(duration=source_duration, seed=seed + index)
            digest.update(asset_id.encode())
            digest.update(samples.astype("<f4", copy=False).tobytes())
            catalog.index_decoded(
                _decoded(samples),
                Asset(
                    asset_id=asset_id,
                    title=f"Synthetic benchmark source {index}",
                    license_id="CC0-1.0",
                    allowed_uses=("evaluation",),
                    metadata={"generator": "auxris.synthetic.v1", "seed": seed + index},
                ),
            )
            sources.append((asset_id, samples))

        excerpt_start = 40 * config.hop_length
        excerpt_samples = int(query_duration * SAMPLE_RATE)
        for source_index, (asset_id, source) in enumerate(sources):
            excerpt = source[excerpt_start : excerpt_start + excerpt_samples]
            for transform_index, (name, transform) in enumerate(transformations.items()):
                rng = np.random.default_rng(seed + source_index * 100 + transform_index)
                query = np.asarray(transform(excerpt.copy(), rng), dtype=np.float32)
                cases.append(
                    _run_case(
                        catalog,
                        case_id=f"positive:{source_index:03d}:{name}",
                        transformation=name,
                        query=query,
                        expected_asset_id=asset_id,
                        minimum_votes=minimum_votes,
                        minimum_coverage=minimum_coverage,
                    )
                )
                _update_case_digest(digest, cases[-1].case_id)

            negative = synthetic_recording(
                duration=query_duration,
                seed=seed + 100_000 + source_index,
            )
            cases.append(
                _run_case(
                    catalog,
                    case_id=f"negative:{source_index:03d}:unrelated",
                    transformation="unrelated_negative",
                    query=negative,
                    expected_asset_id=None,
                    minimum_votes=minimum_votes,
                    minimum_coverage=minimum_coverage,
                )
            )
            _update_case_digest(digest, cases[-1].case_id)

    wall_seconds = time.perf_counter() - started
    return _report(
        cases=cases,
        corpus_sha256=digest.hexdigest(),
        source_count=source_count,
        source_duration=source_duration,
        seed=seed,
        minimum_votes=minimum_votes,
        minimum_coverage=minimum_coverage,
        wall_seconds=wall_seconds,
    )


def _transformations() -> dict[str, Transform]:
    return {
        "clean_excerpt": lambda samples, _rng: samples,
        "unaligned_crop_137_samples": lambda samples, _rng: samples[137:],
        "gain_minus_9db": lambda samples, _rng: samples * np.float32(10 ** (-9 / 20)),
        "noise_20db_snr": _add_noise,
        "resample_8khz_roundtrip": _resample_roundtrip,
        "lowpass_3khz": _lowpass,
        "speed_up_3pct": lambda samples, _rng: resample_poly(samples, 100, 103).astype(np.float32),
    }


def _add_noise(samples: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    signal_rms = float(np.sqrt(np.mean(np.square(samples), dtype=np.float64)))
    noise_rms = signal_rms / 10.0
    noise = rng.normal(0, noise_rms, len(samples)).astype(np.float32)
    return samples + noise


def _resample_roundtrip(samples: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
    downsampled = resample_poly(samples, 320, 441)
    return resample_poly(downsampled, 441, 320)[: len(samples)].astype(np.float32)


def _lowpass(samples: np.ndarray, _rng: np.random.Generator) -> np.ndarray:
    filter_sections = butter(6, 3_000, btype="lowpass", fs=SAMPLE_RATE, output="sos")
    return sosfilt(filter_sections, samples).astype(np.float32)


def _run_case(
    catalog: Catalog,
    *,
    case_id: str,
    transformation: str,
    query: np.ndarray,
    expected_asset_id: str | None,
    minimum_votes: int,
    minimum_coverage: float,
) -> CaseResult:
    started = time.perf_counter()
    fingerprints = catalog.fingerprinter.fingerprint(query)
    matches = catalog.identify_fingerprints(
        fingerprints,
        intended_use="evaluation",
        limit=1,
        minimum_votes=minimum_votes,
        minimum_coverage=minimum_coverage,
    )
    latency_ms = (time.perf_counter() - started) * 1_000
    top = matches[0] if matches else None
    matched_asset_id = top.asset.asset_id if top else None
    return CaseResult(
        case_id=case_id,
        transformation=transformation,
        expected_asset_id=expected_asset_id,
        matched_asset_id=matched_asset_id,
        correct=matched_asset_id == expected_asset_id,
        query_seconds=len(query) / SAMPLE_RATE,
        query_sha256=hashlib.sha256(query.astype("<f4", copy=False).tobytes()).hexdigest(),
        latency_ms=round(latency_ms, 3),
        fingerprints=len(fingerprints),
        aligned_votes=top.evidence.aligned_votes if top else None,
        coverage=top.evidence.coverage if top else None,
    )


def _decoded(samples: np.ndarray) -> DecodedAudio:
    canonical = samples.astype("<f4", copy=False).tobytes()
    return DecodedAudio(
        samples=samples,
        sample_rate=SAMPLE_RATE,
        duration_seconds=len(samples) / SAMPLE_RATE,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _update_case_digest(digest: Any, case_id: str) -> None:
    digest.update(case_id.encode())


def _report(
    *,
    cases: list[CaseResult],
    corpus_sha256: str,
    source_count: int,
    source_duration: float,
    seed: int,
    minimum_votes: int,
    minimum_coverage: float,
    wall_seconds: float,
) -> dict[str, Any]:
    positives = [case for case in cases if case.expected_asset_id is not None]
    negatives = [case for case in cases if case.expected_asset_id is None]
    true_positives = sum(case.correct for case in positives)
    false_negatives = len(positives) - true_positives
    false_positives = sum(case.matched_asset_id is not None for case in negatives)
    wrong_positive_matches = sum(
        case.matched_asset_id is not None and not case.correct for case in positives
    )
    false_positives += wrong_positive_matches
    precision_denominator = true_positives + false_positives
    query_seconds = sum(case.query_seconds for case in cases)
    latencies = np.asarray([case.latency_ms for case in cases], dtype=np.float64)

    by_transformation: dict[str, dict[str, int | float]] = {}
    for transformation in sorted({case.transformation for case in cases}):
        transformed = [case for case in cases if case.transformation == transformation]
        by_transformation[transformation] = {
            "queries": len(transformed),
            "correct": sum(case.correct for case in transformed),
            "recall": round(sum(case.correct for case in transformed) / len(transformed), 6),
        }

    return {
        "schema_version": REPORT_SCHEMA,
        "benchmark_id": "auxris.synthetic-transformations.v1",
        "corpus_sha256": corpus_sha256,
        "engine": {
            "auxris_version": __version__,
            "fingerprint_algorithm": "auxris.constellation.v1",
            "implementation": "python-reference",
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "scipy_version": scipy_version,
            "platform": platform.system().lower(),
            "architecture": platform.machine().lower(),
        },
        "configuration": {
            "source_count": source_count,
            "source_duration_seconds": source_duration,
            "mean_query_duration_seconds": round(query_seconds / len(cases), 6),
            "seed": seed,
            "minimum_votes": minimum_votes,
            "minimum_coverage": minimum_coverage,
        },
        "metrics": {
            "positive_queries": len(positives),
            "negative_queries": len(negatives),
            "true_positives": true_positives,
            "false_negatives": false_negatives,
            "false_positives": false_positives,
            "precision": round(
                true_positives / precision_denominator if precision_denominator else 0.0,
                6,
            ),
            "recall": round(true_positives / len(positives) if positives else 0.0, 6),
            "false_matches_per_query_hour": round(
                false_positives / (query_seconds / 3_600) if query_seconds else 0.0,
                6,
            ),
            "latency_ms_p50": round(float(np.percentile(latencies, 50)), 3),
            "latency_ms_p95": round(float(np.percentile(latencies, 95)), 3),
            "processed_audio_seconds": round(query_seconds, 3),
            "wall_seconds": round(wall_seconds, 3),
            "audio_hours_per_wall_hour": round(query_seconds / wall_seconds, 3),
        },
        "by_transformation": by_transformation,
        "cases": [asdict(case) for case in cases],
        "limitations": [
            "All media is deterministically generated; this is a regression benchmark, "
            "not a real-world accuracy claim.",
            "Catalog size, transformations, thresholds, and corpus digest are part of "
            "every result.",
            "Scores do not determine ownership, infringement, authorship, or whether "
            "audio was AI-generated.",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Run the benchmark CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=int, default=4)
    parser.add_argument("--source-duration", type=float, default=8.0)
    parser.add_argument("--query-duration", type=float, default=4.0)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    report = run_transformation_benchmark(
        source_count=arguments.sources,
        source_duration=arguments.source_duration,
        query_duration=arguments.query_duration,
        seed=arguments.seed,
    )
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
