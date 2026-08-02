from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest
import soundfile as sf
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator

from auxris.core.models import Asset
from auxris.core.tenant_catalog import TenantCatalogRegistry
from auxris.dataset_guard.deletion import (
    DeletionManager,
    DeletionQuotaExceeded,
    DeletionRequest,
)
from auxris.server.app import create_app
from tests.conftest import synthetic_audio, write_audio


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    sf.write(stream, synthetic_audio(seed=109, duration=6), 11_025, format="WAV")
    return stream.getvalue()


def _wait(
    client: TestClient,
    path: str,
    headers: dict[str, str],
    terminal_states: set[str],
) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(path, headers=headers)
        assert response.status_code == 200, response.text
        payload: dict[str, object] = response.json()
        if payload["state"] in terminal_states:
            return payload
        time.sleep(0.02)
    raise AssertionError(f"Job did not finish: {path}")


def test_deletion_jobs_remove_scan_evidence_and_tenant_catalog_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    manifest_root = tmp_path / "manifests"
    media_root.mkdir()
    manifest_root.mkdir()
    control_database = tmp_path / "control.sqlite3"
    audio = media_root / "source.wav"
    audio.write_bytes(_wav_bytes())
    manifest = manifest_root / "scan.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "item_id": "delete-me",
                "media_uri": audio.as_uri(),
                "media_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
            }
        )
        + "\n"
    )

    monkeypatch.delenv("AUXRIS_API_KEY", raising=False)
    monkeypatch.setenv(
        "AUXRIS_API_KEYS_JSON", json.dumps({"alpha-key": "tenant-a", "beta-key": "tenant-b"})
    )
    monkeypatch.setenv("AUXRIS_DB_PATH", str(control_database))
    monkeypatch.setenv("AUXRIS_MANIFEST_ROOT", str(manifest_root))
    monkeypatch.setenv("AUXRIS_MEDIA_ROOT", str(media_root))
    monkeypatch.setenv("AUXRIS_TENANT_CATALOG_ROOT", str(tmp_path / "tenant-catalogs"))
    headers_a = {"Authorization": "Bearer alpha-key"}
    headers_b = {"Authorization": "Bearer beta-key"}

    with TestClient(create_app()) as client:
        indexed = client.post(
            "/v1/assets",
            headers=headers_a,
            data={"asset_id": "private:delete", "allowed_uses": "research"},
            files={"audio": ("source.wav", audio.read_bytes(), "audio/wav")},
        )
        assert indexed.status_code == 201, indexed.text
        scan_response = client.post(
            "/v2/dataset-scans",
            headers=headers_a,
            json={
                "manifest_uri": manifest.as_uri(),
                "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
                "catalog_ids": ["default"],
                "policy_bundle": {
                    "policy_id": "research",
                    "revision": "1",
                    "intended_use": "research",
                },
                "idempotency_key": "scan-before-delete",
            },
        )
        assert scan_response.status_code == 202, scan_response.text
        scan_id = scan_response.json()["scan_id"]
        assert (
            _wait(
                client,
                f"/v2/dataset-scans/{scan_id}",
                headers_a,
                {"completed", "completed_with_errors", "failed"},
            )["state"]
            == "completed"
        )
        item = client.get(f"/v2/dataset-scans/{scan_id}/items", headers=headers_a).json()["items"][
            0
        ]
        review = client.post(
            f"/v2/reviews/{item['evidence_id']}/decisions",
            headers=headers_a,
            json={"outcome": "allow", "reason": "Reviewed before lifecycle deletion"},
        )
        assert review.status_code == 201, review.text

        assert (
            client.post(
                "/v2/deletions",
                headers=headers_b,
                json={
                    "target_type": "scan",
                    "target_id": scan_id,
                    "idempotency_key": "cross-tenant-attempt",
                },
            ).status_code
            == 404
        )
        deletion_response = client.post(
            "/v2/deletions",
            headers=headers_a,
            json={
                "target_type": "scan",
                "target_id": scan_id,
                "idempotency_key": "delete-scan-once",
            },
        )
        assert deletion_response.status_code == 202, deletion_response.text
        deletion_id = deletion_response.json()["deletion_id"]
        repeated = client.post(
            "/v2/deletions",
            headers=headers_a,
            json={
                "target_type": "scan",
                "target_id": scan_id,
                "idempotency_key": "delete-scan-once",
            },
        )
        assert repeated.status_code == 202
        assert repeated.json()["created"] is False
        assert (
            client.post(
                "/v2/deletions",
                headers=headers_a,
                json={
                    "target_type": "catalog_asset",
                    "target_id": "private:delete",
                    "idempotency_key": "delete-scan-once",
                },
            ).status_code
            == 409
        )
        deleted_scan = _wait(
            client,
            f"/v2/deletions/{deletion_id}",
            headers_a,
            {"completed", "failed"},
        )
        assert deleted_scan["state"] == "completed"
        assert deleted_scan["disposition"] == "logical_delete"
        assert deleted_scan["target_id"] is None
        assert deleted_scan["target_sha256"] == hashlib.sha256(scan_id.encode()).hexdigest()
        deletion_schema = json.loads(
            (
                Path(__file__).parents[1] / "schemas/auxris/dataset/v1/deletion-job.schema.json"
            ).read_text()
        )
        Draft202012Validator(deletion_schema).validate(deleted_scan)
        assert client.get(f"/v2/deletions/{deletion_id}", headers=headers_b).status_code == 404
        assert client.get(f"/v2/dataset-scans/{scan_id}", headers=headers_a).status_code == 404

        asset_deletion = client.post(
            "/v2/deletions",
            headers=headers_a,
            json={
                "target_type": "catalog_asset",
                "target_id": "private:delete",
                "idempotency_key": "delete-asset-once",
            },
        )
        assert asset_deletion.status_code == 202, asset_deletion.text
        asset_deletion_id = asset_deletion.json()["deletion_id"]
        assert (
            _wait(
                client,
                f"/v2/deletions/{asset_deletion_id}",
                headers_a,
                {"completed", "failed"},
            )["state"]
            == "completed"
        )
        assert client.get("/v1/catalog", headers=headers_a).json()["stats"]["assets"] == 0

    with sqlite3.connect(control_database) as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_scan_items WHERE scan_id = ?", (scan_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_review_decisions WHERE evidence_id = ?",
                (item["evidence_id"],),
            ).fetchone()[0]
            == 0
        )
        tombstone = connection.execute(
            "SELECT target_id, target_sha256 FROM dataset_deletion_jobs WHERE deletion_id = ?",
            (deletion_id,),
        ).fetchone()
        assert tombstone == (None, hashlib.sha256(scan_id.encode()).hexdigest())
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM dataset_deletion_audit WHERE deletion_id = ?",
                (deletion_id,),
            ).fetchone()[0]
            >= 3
        )


def test_deletion_queue_is_bounded_recoverable_and_retryable(tmp_path: Path) -> None:
    control_database = tmp_path / "control.sqlite3"
    registry = TenantCatalogRegistry(control_database, tmp_path / "tenant-catalogs")
    catalog = registry.get_catalog("tenant-a")
    first = write_audio(tmp_path / "first.wav", synthetic_audio(seed=301, duration=6))
    second = write_audio(tmp_path / "second.wav", synthetic_audio(seed=302, duration=6))
    catalog.index_path(first, Asset(asset_id="first"))
    catalog.index_path(second, Asset(asset_id="second"))
    manager = DeletionManager(control_database, registry, max_queued_per_tenant=1)

    deletion, created = manager.create_deletion(
        "tenant-a",
        DeletionRequest(
            target_type="catalog_asset",
            target_id="first",
            idempotency_key="delete-first",
        ),
    )
    assert created is True
    with pytest.raises(DeletionQuotaExceeded):
        manager.create_deletion(
            "tenant-a",
            DeletionRequest(
                target_type="catalog_asset",
                target_id="second",
                idempotency_key="delete-second",
            ),
        )

    with sqlite3.connect(control_database) as connection:
        connection.execute(
            "UPDATE dataset_deletion_jobs SET state = 'running' WHERE deletion_id = ?",
            (deletion["deletion_id"],),
        )
    assert manager.recover_incomplete_jobs() == 1
    with sqlite3.connect(control_database) as connection:
        connection.execute(
            """
            UPDATE dataset_deletion_jobs SET state = 'failed', last_error = 'transient'
            WHERE deletion_id = ?
            """,
            (deletion["deletion_id"],),
        )
    retried = manager.retry_deletion("tenant-a", str(deletion["deletion_id"]))
    assert retried["state"] == "queued"
    assert manager.run_next_deletion() is True
    completed = manager.get_deletion("tenant-a", str(deletion["deletion_id"]))
    assert completed["state"] == "completed"
    assert completed["target_id"] is None
    assert catalog.get_asset("first") is None
    assert catalog.get_asset("second") is not None
