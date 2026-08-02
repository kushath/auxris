from __future__ import annotations

import hashlib
import io
import json
import time
from pathlib import Path

import soundfile as sf
from fastapi.testclient import TestClient

from auxris.core.index import Catalog
from auxris.core.models import Asset
from auxris.core.tenant_catalog import TenantCatalogRegistry
from auxris.server.app import create_app
from tests.conftest import synthetic_audio, write_audio


def _wav_bytes(seed: int = 83) -> bytes:
    stream = io.BytesIO()
    sf.write(stream, synthetic_audio(seed=seed, duration=6), 11_025, format="WAV", subtype="PCM_16")
    return stream.getvalue()


def _wait_for_scan(client: TestClient, scan_id: str, headers: dict[str, str]) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/v2/dataset-scans/{scan_id}", headers=headers)
        assert response.status_code == 200
        scan: dict[str, object] = response.json()
        if scan["state"] in {"completed", "completed_with_errors", "failed"}:
            return scan
        time.sleep(0.02)
    raise AssertionError("Dataset scan did not reach a terminal state")


def test_registry_uses_separate_hashed_catalog_paths(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.sqlite3"
    legacy = Catalog(legacy_path)
    source = write_audio(tmp_path / "source.wav", synthetic_audio(seed=41, duration=6))
    legacy.index_path(source, Asset(asset_id="existing-default"))
    registry = TenantCatalogRegistry(legacy_path, tmp_path / "catalogs")

    default = registry.get_catalog("default")
    tenant_a = registry.get_catalog("tenant-a")
    tenant_a.index_path(source, Asset(asset_id="private-a", allowed_uses=("research",)))
    tenant_b = registry.get_catalog("tenant-b")
    path_safe = registry.get_catalog("../../escape")

    assert default.path == legacy_path.resolve()
    assert default.get_asset("existing-default") is not None
    assert tenant_a.path != tenant_b.path
    assert tenant_a.path.parent == registry.catalog_root
    assert tenant_b.path.parent == registry.catalog_root
    assert path_safe.path.parent == registry.catalog_root
    assert "tenant-a" not in tenant_a.path.name
    assert "escape" not in path_safe.path.name
    assert tenant_b.stats()["assets"] == 0
    assert tenant_b.identify_path(source, intended_use="research") == []


def test_api_and_dataset_guard_do_not_leak_candidates_between_tenants(
    monkeypatch: object, tmp_path: Path
) -> None:
    media_root = tmp_path / "media"
    manifest_root = tmp_path / "manifests"
    catalog_root = tmp_path / "tenant-catalogs"
    media_root.mkdir()
    manifest_root.mkdir()
    media = media_root / "source.wav"
    media.write_bytes(_wav_bytes())
    manifest = manifest_root / "scan.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "item_id": "candidate-isolation",
                "media_uri": media.as_uri(),
                "media_sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n"
    )

    monkeypatch.delenv("AUXRIS_API_KEY", raising=False)  # type: ignore[attr-defined]
    monkeypatch.setenv(  # type: ignore[attr-defined]
        "AUXRIS_API_KEYS_JSON", json.dumps({"alpha-key": "tenant-a", "beta-key": "tenant-b"})
    )
    monkeypatch.setenv("AUXRIS_DB_PATH", str(tmp_path / "control.sqlite3"))  # type: ignore[attr-defined]
    monkeypatch.setenv("AUXRIS_MANIFEST_ROOT", str(manifest_root))  # type: ignore[attr-defined]
    monkeypatch.setenv("AUXRIS_MEDIA_ROOT", str(media_root))  # type: ignore[attr-defined]
    monkeypatch.setenv("AUXRIS_TENANT_CATALOG_ROOT", str(catalog_root))  # type: ignore[attr-defined]

    headers_a = {"Authorization": "Bearer alpha-key"}
    headers_b = {"Authorization": "Bearer beta-key"}
    with TestClient(create_app()) as client:
        indexed = client.post(
            "/v1/assets",
            headers=headers_a,
            data={"asset_id": "tenant-a:private", "allowed_uses": "research"},
            files={"audio": ("source.wav", media.read_bytes(), "audio/wav")},
        )
        assert indexed.status_code == 201, indexed.text
        assert client.get("/v1/catalog", headers=headers_a).json()["stats"]["assets"] == 1
        assert client.get("/v1/catalog", headers=headers_b).json()["stats"]["assets"] == 0

        identify_b = client.post(
            "/v1/identify",
            headers=headers_b,
            files={"audio": ("query.wav", media.read_bytes(), "audio/wav")},
        )
        assert identify_b.status_code == 200
        assert identify_b.json()["matched"] is False

        request = {
            "manifest_uri": manifest.as_uri(),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "catalog_ids": ["default"],
            "policy_bundle": {
                "policy_id": "training",
                "revision": "1",
                "intended_use": "research",
            },
        }
        scan_a = client.post(
            "/v2/dataset-scans",
            headers=headers_a,
            json={**request, "idempotency_key": "tenant-a-scan"},
        ).json()
        scan_b = client.post(
            "/v2/dataset-scans",
            headers=headers_b,
            json={**request, "idempotency_key": "tenant-b-scan"},
        ).json()
        assert _wait_for_scan(client, scan_a["scan_id"], headers_a)["state"] == "completed"
        assert _wait_for_scan(client, scan_b["scan_id"], headers_b)["state"] == "completed"

        item_a = client.get(
            f"/v2/dataset-scans/{scan_a['scan_id']}/items", headers=headers_a
        ).json()["items"][0]
        item_b = client.get(
            f"/v2/dataset-scans/{scan_b['scan_id']}/items", headers=headers_b
        ).json()["items"][0]
        assert item_a["matched"] is True
        assert item_a["decision"] == "allow"
        assert item_b["matched"] is False
        assert item_b["decision"] == "review"
        assert (
            client.get(
                f"/v2/dataset-scans/{scan_a['scan_id']}/items", headers=headers_b
            ).status_code
            == 404
        )
