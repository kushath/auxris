"""Durable SQLite catalog and temporal-offset fingerprint search."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

from auxris.core.audio import DecodedAudio, decode_path
from auxris.core.fingerprint import FingerprintConfig, Fingerprinter
from auxris.core.models import Asset, Fingerprint, Match, MatchEvidence
from auxris.core.policy import evaluate_policy

SCHEMA_VERSION = 1


class CatalogError(RuntimeError):
    """Raised when the durable catalog cannot satisfy an operation."""


class Catalog:
    """A single-node, durable catalog suitable for local and small-team use."""

    def __init__(
        self,
        path: str | Path,
        *,
        config: FingerprintConfig | None = None,
    ) -> None:
        self.path = Path(path)
        self.config = config or FingerprintConfig()
        self.fingerprinter = Fingerprinter(self.config)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assets (
                    asset_id TEXT PRIMARY KEY,
                    title TEXT,
                    creator TEXT,
                    license_id TEXT,
                    source_uri TEXT,
                    c2pa_manifest_uri TEXT,
                    allowed_uses_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    duration_seconds REAL NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    fingerprint_algorithm TEXT NOT NULL,
                    fingerprint_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS fingerprints (
                    hash BLOB NOT NULL,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    frame INTEGER NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_fingerprints_hash
                ON fingerprints(hash);

                CREATE INDEX IF NOT EXISTS idx_fingerprints_asset
                ON fingerprints(asset_id);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.commit()

    def index_path(self, path: str | Path, asset: Asset) -> dict[str, object]:
        audio = decode_path(path, self.config.sample_rate)
        return self.index_decoded(audio, asset)

    def index_decoded(self, audio: DecodedAudio, asset: Asset) -> dict[str, object]:
        fingerprints = self.fingerprinter.fingerprint(audio.samples)
        if not fingerprints:
            raise CatalogError(
                "Audio produced no fingerprints; provide a longer, non-silent recording"
            )

        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM assets WHERE asset_id = ?", (asset.asset_id,))
                connection.execute(
                    """
                    INSERT INTO assets(
                        asset_id, title, creator, license_id, source_uri,
                        c2pa_manifest_uri, allowed_uses_json, metadata_json,
                        duration_seconds, content_sha256, fingerprint_algorithm,
                        fingerprint_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        asset.asset_id,
                        asset.title,
                        asset.creator,
                        asset.license_id,
                        asset.source_uri,
                        asset.c2pa_manifest_uri,
                        json.dumps(asset.allowed_uses),
                        json.dumps(asset.metadata, sort_keys=True),
                        audio.duration_seconds,
                        audio.sha256,
                        self.fingerprinter.algorithm,
                        len(fingerprints),
                    ),
                )
                connection.executemany(
                    "INSERT INTO fingerprints(hash, asset_id, frame) VALUES (?, ?, ?)",
                    ((item.hash_bytes, asset.asset_id, item.frame) for item in fingerprints),
                )
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise CatalogError(f"Could not index asset: {exc}") from exc

        return {
            "asset_id": asset.asset_id,
            "duration_seconds": round(audio.duration_seconds, 3),
            "fingerprints": len(fingerprints),
            "content_sha256": audio.sha256,
            "algorithm": self.fingerprinter.algorithm,
        }

    def identify_path(
        self,
        path: str | Path,
        *,
        intended_use: str | None = None,
        limit: int = 5,
        minimum_votes: int = 8,
        minimum_coverage: float = 0.015,
    ) -> list[Match]:
        audio = decode_path(path, self.config.sample_rate)
        fingerprints = self.fingerprinter.fingerprint(audio.samples)
        return self.identify_fingerprints(
            fingerprints,
            intended_use=intended_use,
            limit=limit,
            minimum_votes=minimum_votes,
            minimum_coverage=minimum_coverage,
        )

    def identify_fingerprints(
        self,
        fingerprints: Sequence[Fingerprint],
        *,
        intended_use: str | None = None,
        limit: int = 5,
        minimum_votes: int = 8,
        minimum_coverage: float = 0.015,
    ) -> list[Match]:
        if not fingerprints:
            return []

        query_frames: dict[bytes, list[int]] = defaultdict(list)
        for item in fingerprints:
            query_frames[item.hash_bytes].append(item.frame)

        votes: Counter[tuple[str, int]] = Counter()
        aligned_frames: dict[tuple[str, int], list[tuple[int, int]]] = defaultdict(list)
        hashes = list(query_frames)

        with self._connection() as connection:
            for chunk in _chunks(hashes, 800):
                placeholders = ",".join("?" for _ in chunk)
                query = (
                    "SELECT hash, asset_id, frame FROM fingerprints "  # noqa: S608
                    f"WHERE hash IN ({placeholders})"
                )
                rows = connection.execute(query, chunk)
                for row in rows:
                    hash_bytes = bytes(row["hash"])
                    source_frame = int(row["frame"])
                    asset_id = str(row["asset_id"])
                    for query_frame in query_frames[hash_bytes]:
                        offset = source_frame - query_frame
                        key = (asset_id, offset)
                        votes[key] += 1
                        aligned_frames[key].append((query_frame, source_frame))

            best_by_asset: dict[str, tuple[int, int]] = {}
            for (asset_id, offset), count in votes.items():
                if count > best_by_asset.get(asset_id, (0, 0))[0]:
                    best_by_asset[asset_id] = (count, offset)

            ranked: list[Match] = []
            for asset_id, (aligned_votes, offset) in best_by_asset.items():
                coverage = aligned_votes / len(fingerprints)
                if aligned_votes < minimum_votes or coverage < minimum_coverage:
                    continue
                asset = self._get_asset(connection, asset_id)
                pairs = aligned_frames[(asset_id, offset)]
                query_start = min(pair[0] for pair in pairs)
                query_end = max(pair[0] for pair in pairs)
                source_start = min(pair[1] for pair in pairs)
                source_end = max(pair[1] for pair in pairs)
                evidence = MatchEvidence(
                    aligned_votes=aligned_votes,
                    query_fingerprints=len(fingerprints),
                    coverage=round(coverage, 6),
                    offset_frames=offset,
                    query_start_seconds=round(query_start * self.config.frame_seconds, 3),
                    query_end_seconds=round(query_end * self.config.frame_seconds, 3),
                    source_start_seconds=round(source_start * self.config.frame_seconds, 3),
                    source_end_seconds=round(source_end * self.config.frame_seconds, 3),
                )
                ranked.append(
                    Match(
                        asset=asset,
                        score=round(min(1.0, coverage * 5.0), 6),
                        evidence=evidence,
                        policy=evaluate_policy(asset, intended_use),
                    )
                )

        return sorted(
            ranked,
            key=lambda match: (match.evidence.aligned_votes, match.evidence.coverage),
            reverse=True,
        )[:limit]

    def get_asset(self, asset_id: str) -> Asset | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM assets WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
            return None if row is None else self._asset_from_row(row)

    def list_assets(self, *, limit: int = 100, offset: int = 0) -> list[Asset]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM assets ORDER BY created_at DESC, asset_id LIMIT ? OFFSET ?",
                (min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._asset_from_row(row) for row in rows]

    def stats(self) -> dict[str, int | str]:
        with self._connection() as connection:
            asset_count = connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            fingerprint_count = connection.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[
                0
            ]
        return {
            "schema_version": SCHEMA_VERSION,
            "fingerprint_algorithm": self.fingerprinter.algorithm,
            "assets": int(asset_count),
            "fingerprints": int(fingerprint_count),
        }

    def remove_asset(self, asset_id: str) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM assets WHERE asset_id = ?", (asset_id,))
            connection.commit()
            return cursor.rowcount > 0

    def _get_asset(self, connection: sqlite3.Connection, asset_id: str) -> Asset:
        row = connection.execute(
            "SELECT * FROM assets WHERE asset_id = ?",
            (asset_id,),
        ).fetchone()
        if row is None:
            raise CatalogError(f"Catalog index refers to missing asset: {asset_id}")
        return self._asset_from_row(row)

    @staticmethod
    def _asset_from_row(row: sqlite3.Row) -> Asset:
        asset = Asset(
            asset_id=str(row["asset_id"]),
            title=row["title"],
            creator=row["creator"],
            license_id=row["license_id"],
            source_uri=row["source_uri"],
            c2pa_manifest_uri=row["c2pa_manifest_uri"],
            allowed_uses=tuple(json.loads(row["allowed_uses_json"])),
            metadata=dict(json.loads(row["metadata_json"])),
        )
        return replace(
            asset,
            metadata={
                **asset.metadata,
                "duration_seconds": float(row["duration_seconds"]),
                "content_sha256": str(row["content_sha256"]),
                "fingerprint_count": int(row["fingerprint_count"]),
            },
        )


def _chunks(values: Sequence[bytes], size: int) -> Iterable[Sequence[bytes]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
