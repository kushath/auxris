from __future__ import annotations

from pathlib import Path

from auxris.core.fingerprint import FingerprintConfig
from auxris.core.index import Catalog
from auxris.core.models import Asset
from tests.conftest import synthetic_audio, write_audio


def test_index_restart_identify_and_reject_unknown(tmp_path: Path) -> None:
    config = FingerprintConfig()
    source = synthetic_audio(seed=11)
    source_path = write_audio(tmp_path / "source.wav", source)
    database = tmp_path / "catalog.sqlite3"

    catalog = Catalog(database, config=config)
    indexed = catalog.index_path(
        source_path,
        Asset(
            asset_id="studio:source:11",
            title="Licensed source",
            license_id="internal-v1",
            allowed_uses=("research",),
        ),
    )
    assert indexed["fingerprints"] > 100

    del catalog
    restarted = Catalog(database, config=config)
    start_sample = 40 * config.hop_length
    query = source[start_sample : start_sample + 5 * config.sample_rate]
    query_path = write_audio(tmp_path / "query.wav", query * 0.72)

    matches = restarted.identify_path(query_path, intended_use="commercial-release")
    assert matches
    assert matches[0].asset.asset_id == "studio:source:11"
    assert matches[0].evidence.aligned_votes >= 8
    assert matches[0].policy.decision == "not_allowed"

    unknown_path = write_audio(tmp_path / "unknown.wav", synthetic_audio(seed=997, duration=5))
    assert restarted.identify_path(unknown_path) == []


def test_reindex_replaces_fingerprints(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    catalog = Catalog(database)
    first = write_audio(tmp_path / "first.wav", synthetic_audio(seed=1, duration=6))
    second = write_audio(tmp_path / "second.wav", synthetic_audio(seed=2, duration=6))
    asset = Asset(asset_id="replace-me")
    catalog.index_path(first, asset)
    catalog.index_path(second, asset)
    assert catalog.stats()["assets"] == 1
    assert catalog.get_asset("replace-me") is not None
