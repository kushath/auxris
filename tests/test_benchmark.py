from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from auxris.evaluation.benchmark import run_transformation_benchmark


def test_synthetic_benchmark_is_reproducible_and_schema_valid() -> None:
    report = run_transformation_benchmark(
        source_count=2,
        source_duration=6,
        query_duration=3,
        seed=19,
    )
    repeated = run_transformation_benchmark(
        source_count=2,
        source_duration=6,
        query_duration=3,
        seed=19,
    )

    assert report["corpus_sha256"] == repeated["corpus_sha256"]
    assert report["corpus_sha256"] == (
        "25c6b97b579065f46cf7d650b9244b2a675644b42a446a0b3b183c971aa99ebc"
    )
    assert report["metrics"]["positive_queries"] == 14
    assert report["metrics"]["negative_queries"] == 2
    assert report["metrics"]["precision"] >= 0.0
    assert report["metrics"]["recall"] >= 0.0
    assert len(report["by_transformation"]) == 8
    assert report["by_transformation"]["clean_excerpt"]["recall"] == 1.0
    assert report["by_transformation"]["unrelated_negative"]["recall"] == 1.0
    assert report["limitations"]

    root = Path(__file__).parents[1]
    schema = json.loads(
        (root / "schemas" / "auxris" / "benchmark" / "v1" / "report.schema.json").read_text(
            encoding="utf-8"
        )
    )
    Draft202012Validator(schema).validate(report)
