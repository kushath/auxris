from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from auxris.core.index import Catalog
from auxris.core.models import Asset
from auxris.dataset_guard import (
    DatasetGuard,
    DatasetGuardConflict,
    DatasetGuardError,
    DatasetGuardNotFound,
    DatasetGuardQuotaExceeded,
    ScanRequest,
)
from auxris.server.app import create_app
from tests.conftest import synthetic_audio, write_audio


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(path: Path, items: list[dict[str, str]]) -> tuple[str, str]:
    path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in items))
    return path.as_uri(), _sha256(path)


def _request(manifest_uri: str, manifest_sha256: str, key: str = "scan-one") -> ScanRequest:
    return ScanRequest(
        manifest_uri=manifest_uri,
        manifest_sha256=manifest_sha256,
        catalog_ids=("default",),
        policy_bundle={
            "policy_id": "training-policy",
            "revision": "1",
            "intended_use": "research",
        },
        detector_profile="constellation-v1",
        idempotency_key=key,
    )


def _guard_fixture(tmp_path: Path) -> tuple[DatasetGuard, Path, Path]:
    media_root = tmp_path / "media"
    manifest_root = tmp_path / "manifests"
    media_root.mkdir()
    manifest_root.mkdir()
    database = tmp_path / "auxris.sqlite3"
    catalog = Catalog(database)
    source = write_audio(media_root / "source.wav", synthetic_audio(seed=31, duration=8))
    catalog.index_path(source, Asset(asset_id="licensed:31", allowed_uses=("research",)))
    return (
        DatasetGuard(
            database,
            catalog,
            manifest_root=manifest_root,
            media_root=media_root,
            max_items_per_scan=10,
        ),
        manifest_root,
        media_root,
    )


def test_scan_is_idempotent_tenant_scoped_and_exports_immutable_evidence(
    tmp_path: Path,
) -> None:
    guard, manifest_root, media_root = _guard_fixture(tmp_path)
    query = write_audio(
        media_root / "query.wav",
        synthetic_audio(seed=31, duration=8)[: 5 * 11_025] * 0.75,
    )
    unknown = write_audio(media_root / "unknown.wav", np.zeros(5 * 11_025, dtype=np.float32))
    uri, digest = _manifest(
        manifest_root / "scan.jsonl",
        [
            {"item_id": "known", "media_uri": query.as_uri(), "media_sha256": _sha256(query)},
            {
                "item_id": "unknown",
                "media_uri": unknown.as_uri(),
                "media_sha256": _sha256(unknown),
            },
        ],
    )

    created, is_new = guard.create_scan("tenant-a", _request(uri, digest))
    repeated, repeated_is_new = guard.create_scan("tenant-a", _request(uri, digest))
    assert is_new is True
    assert repeated_is_new is False
    assert repeated["scan_id"] == created["scan_id"]
    with pytest.raises(DatasetGuardConflict):
        guard.create_scan(
            "tenant-a",
            replace(
                _request(uri, digest, key="scan-one"),
                policy_bundle={"intended_use": "training"},
            ),
        )
    with pytest.raises(DatasetGuardNotFound):
        guard.get_scan("tenant-b", created["scan_id"])
    with pytest.raises(DatasetGuardError, match="unsupported fields"):
        guard.create_scan(
            "tenant-a",
            replace(
                _request(uri, digest, key="misspelled-policy"),
                policy_bundle={"intended_ues": "training"},
            ),
        )
    with pytest.raises(DatasetGuardError, match="detector_profile"):
        guard.create_scan(
            "tenant-a",
            replace(_request(uri, digest, key="unknown-detector"), detector_profile="magic-v1"),
        )

    assert guard.run_next_scan() is True
    finished = guard.get_scan("tenant-a", created["scan_id"])
    assert finished["state"] == "completed"
    assert finished["processed_items"] == 2
    assert finished["matched_items"] == 1

    items = guard.list_items("tenant-a", created["scan_id"])
    assert [item["decision"] for item in items] == ["allow", "review"]
    evidence_ids = [item["evidence_id"] for item in items]
    with sqlite3.connect(guard.database) as connection:
        connection.execute(
            "UPDATE dataset_scans SET state = 'queued', completed_at = NULL WHERE scan_id = ?",
            (created["scan_id"],),
        )
    assert guard.run_next_scan() is True
    assert [
        item["evidence_id"] for item in guard.list_items("tenant-a", created["scan_id"])
    ] == evidence_ids
    evidence_before = items[1]["evidence"]
    schema = json.loads(
        (
            Path(__file__).parents[1] / "schemas/auxris/evidence/v1/evidence-envelope.schema.json"
        ).read_text()
    )
    for item in items:
        Draft202012Validator(schema).validate(item["evidence"])

    decision = guard.add_review_decision(
        "tenant-a",
        items[1]["evidence_id"],
        outcome="allow",
        reason="Partner supplied a license after review",
        reviewer="reviewer@example.test",
    )
    assert decision["outcome"] == "allow"
    with pytest.raises(DatasetGuardNotFound):
        guard.add_review_decision(
            "tenant-b",
            items[1]["evidence_id"],
            outcome="allow",
            reason="Must not cross tenant boundary",
            reviewer=None,
        )
    after_review = guard.list_items("tenant-a", created["scan_id"])[1]
    assert after_review["evidence"] == evidence_before
    assert after_review["review_decisions"][0]["outcome"] == "allow"

    records = [
        json.loads(line) for line in guard.export_jsonl("tenant-a", created["scan_id"]).splitlines()
    ]
    export_schema = json.loads(
        (
            Path(__file__).parents[1] / "schemas/auxris/dataset/v1/export-record.schema.json"
        ).read_text()
    )
    for record in records:
        Draft202012Validator(export_schema).validate(record)
    assert records[0]["record_type"] == "scan"
    assert [record["record_type"] for record in records[1:]] == ["item", "item"]
    with sqlite3.connect(guard.database) as connection:
        evidence_rows = connection.execute(
            "SELECT COUNT(*) FROM dataset_scan_items WHERE scan_id = ? AND evidence_id IS NOT NULL",
            (created["scan_id"],),
        ).fetchone()[0]
        audit_rows = connection.execute(
            "SELECT COUNT(*) FROM dataset_audit_events WHERE scan_id = ?",
            (created["scan_id"],),
        ).fetchone()[0]
    assert evidence_rows == 2
    assert audit_rows >= 5


def test_manifest_bounds_root_confinement_resume_and_cancellation(tmp_path: Path) -> None:
    guard, manifest_root, media_root = _guard_fixture(tmp_path)
    media = write_audio(media_root / "item.wav", synthetic_audio(seed=77, duration=5))
    uri, digest = _manifest(
        manifest_root / "one.jsonl",
        [{"item_id": "one", "media_uri": media.as_uri()}],
    )
    scan, _ = guard.create_scan("tenant-a", _request(uri, digest, key="resume"))
    with sqlite3.connect(guard.database) as connection:
        connection.execute(
            "UPDATE dataset_scans SET state = 'running' WHERE scan_id = ?", (scan["scan_id"],)
        )
    assert guard.recover_incomplete_scans() == 1
    assert guard.run_next_scan() is True
    assert guard.get_scan("tenant-a", scan["scan_id"])["state"] == "completed"

    cancelled, _ = guard.create_scan("tenant-a", _request(uri, digest, key="cancel"))
    assert guard.request_cancellation("tenant-a", cancelled["scan_id"])["state"] == "cancelled"
    assert guard.run_next_scan() is False

    outside = tmp_path / "outside.jsonl"
    outside_uri, outside_digest = _manifest(
        outside, [{"item_id": "outside", "media_uri": media.as_uri()}]
    )
    with pytest.raises(DatasetGuardError, match="outside"):
        guard.create_scan("tenant-a", _request(outside_uri, outside_digest, key="outside"))

    too_many_uri, too_many_digest = _manifest(
        manifest_root / "too-many.jsonl",
        [{"item_id": f"item-{index}", "media_uri": media.as_uri()} for index in range(2)],
    )
    bounded = DatasetGuard(
        guard.database,
        guard.catalog,
        manifest_root=manifest_root,
        media_root=media_root,
        max_items_per_scan=1,
    )
    with pytest.raises(DatasetGuardQuotaExceeded):
        bounded.create_scan("tenant-a", _request(too_many_uri, too_many_digest, key="too-many"))

    media_bounded = DatasetGuard(
        guard.database,
        guard.catalog,
        manifest_root=manifest_root,
        media_root=media_root,
        max_media_bytes=1,
        max_items_per_scan=10,
    )
    oversized, _ = media_bounded.create_scan(
        "tenant-a", _request(uri, digest, key="oversized-media")
    )
    assert media_bounded.run_next_scan() is True
    assert media_bounded.get_scan("tenant-a", oversized["scan_id"])["state"] == (
        "completed_with_errors"
    )
    assert "byte limit" in media_bounded.list_items("tenant-a", oversized["scan_id"])[0]["error"]


def test_dataset_guard_api_keeps_scan_records_between_tenants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    manifest_root = tmp_path / "manifests"
    media_root.mkdir()
    manifest_root.mkdir()
    media = write_audio(media_root / "unknown.wav", synthetic_audio(seed=501, duration=5))
    manifest_uri, manifest_digest = _manifest(
        manifest_root / "api.jsonl",
        [{"item_id": "api-item", "media_uri": media.as_uri(), "media_sha256": _sha256(media)}],
    )
    monkeypatch.delenv("AUXRIS_API_KEY", raising=False)
    monkeypatch.setenv(
        "AUXRIS_API_KEYS_JSON", json.dumps({"alpha-key": "tenant-a", "beta-key": "tenant-b"})
    )
    monkeypatch.setenv("AUXRIS_DB_PATH", str(tmp_path / "api.sqlite3"))
    monkeypatch.setenv("AUXRIS_MANIFEST_ROOT", str(manifest_root))
    monkeypatch.setenv("AUXRIS_MEDIA_ROOT", str(media_root))

    headers_a = {"Authorization": "Bearer alpha-key"}
    headers_b = {"Authorization": "Bearer beta-key"}
    with TestClient(create_app()) as client:
        response = client.post(
            "/v2/dataset-scans",
            headers=headers_a,
            json={
                "manifest_uri": manifest_uri,
                "manifest_sha256": manifest_digest,
                "catalog_ids": ["default"],
                "policy_bundle": {"policy_id": "review", "revision": "1"},
                "idempotency_key": "api-scan",
            },
        )
        assert response.status_code == 202, response.text
        scan_id = response.json()["scan_id"]
        assert client.get(f"/v2/dataset-scans/{scan_id}", headers=headers_b).status_code == 404
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = client.get(f"/v2/dataset-scans/{scan_id}", headers=headers_a).json()
            if current["state"] in {"completed", "completed_with_errors", "failed"}:
                break
            time.sleep(0.02)
        assert current["state"] == "completed"
        items = client.get(f"/v2/dataset-scans/{scan_id}/items", headers=headers_a).json()
        assert items["items"][0]["decision"] == "review"
        exported = client.get(f"/v2/dataset-scans/{scan_id}/export.jsonl", headers=headers_a)
        assert exported.status_code == 200
        assert exported.headers["content-type"].startswith("application/x-ndjson")
