from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).parents[1]


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        value: dict[str, Any] = json.load(handle)
    return value


def test_public_schemas_have_stable_ids_and_versions() -> None:
    schemas = sorted((ROOT / "schemas" / "auxris").glob("**/*.schema.json"))
    assert len(schemas) == 7
    for path in schemas:
        schema = load_json(path)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].startswith("https://auxris.org/schemas/")
        if "oneOf" in schema:
            assert schema["$defs"]["scan"]["additionalProperties"] is False
            assert schema["$defs"]["item"]["additionalProperties"] is False
        else:
            assert schema["additionalProperties"] is False
        Draft202012Validator.check_schema(schema)


def test_evidence_example_uses_declared_contract() -> None:
    example = load_json(ROOT / "schemas" / "examples" / "evidence-envelope.json")
    schema = load_json(
        ROOT / "schemas" / "auxris" / "evidence" / "v1" / "evidence-envelope.schema.json"
    )
    Draft202012Validator(schema).validate(example)
    assert example["schema_version"] == "auxris.evidence.v1"
    assert len(example["subject"]["sha256"]) == 64
    assert example["policy"]["decision"] in {
        "allow",
        "review",
        "quarantine",
        "block",
        "not_evaluated",
    }
    assert "confidence" not in example["findings"][0]
