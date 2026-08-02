from __future__ import annotations

import io
from pathlib import Path

import soundfile as sf
from fastapi.testclient import TestClient

from auxris.server.app import create_app
from tests.conftest import synthetic_audio


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    sf.write(stream, synthetic_audio(duration=6), 11_025, format="WAV", subtype="PCM_16")
    return stream.getvalue()


def test_server_requires_key_and_indexes(monkeypatch: object, tmp_path: Path) -> None:
    monkeypatch.setenv("AUXRIS_API_KEY", "test-only-secret")  # type: ignore[attr-defined]
    monkeypatch.setenv("AUXRIS_DB_PATH", str(tmp_path / "api.sqlite3"))  # type: ignore[attr-defined]
    with TestClient(create_app()) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/v1/catalog").status_code == 401
        response = client.post(
            "/v1/assets",
            headers={"Authorization": "Bearer test-only-secret"},
            data={"asset_id": "api:asset:1", "allowed_uses": "research"},
            files={"audio": ("source.wav", _wav_bytes(), "audio/wav")},
        )
        assert response.status_code == 201, response.text
        assert response.json()["fingerprints"] > 0
        catalog = client.get(
            "/v1/catalog",
            headers={"Authorization": "Bearer test-only-secret"},
        )
        assert catalog.json()["stats"]["assets"] == 1
