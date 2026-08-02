"""SQLite reference implementation of the experimental Dataset Guard job API."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import unquote, urlparse

from auxris import __version__
from auxris.core.audio import AudioDecodeError
from auxris.core.index import Catalog, CatalogError

ScanState = Literal[
    "queued",
    "running",
    "completed",
    "completed_with_errors",
    "cancelled",
    "failed",
]
Decision = Literal["allow", "review", "quarantine", "block", "not_evaluated"]
TERMINAL_STATES = frozenset({"completed", "completed_with_errors", "cancelled", "failed"})
DATASET_SCHEMA_VERSION = 1


class DatasetGuardError(RuntimeError):
    """Base error for invalid Dataset Guard operations."""


class DatasetGuardNotFound(DatasetGuardError):
    """Raised when a tenant cannot access the requested record."""


class DatasetGuardConflict(DatasetGuardError):
    """Raised when an idempotency key is reused for another request."""


class DatasetGuardQuotaExceeded(DatasetGuardError):
    """Raised when an explicit service resource limit is exceeded."""


@dataclass(frozen=True, slots=True)
class ScanRequest:
    """Framework-independent request to create a dataset scan."""

    manifest_uri: str
    manifest_sha256: str
    catalog_ids: tuple[str, ...]
    policy_bundle: Mapping[str, Any]
    detector_profile: str
    idempotency_key: str
    prior_scan_id: str | None = None


@dataclass(frozen=True, slots=True)
class ManifestItem:
    ordinal: int
    item_id: str
    media_uri: str
    media_sha256: str | None
    intended_use: str | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


class DatasetGuard:
    """Durable single-node Dataset Guard with explicit tenant and filesystem bounds."""

    def __init__(
        self,
        database: str | Path,
        catalog: Catalog,
        *,
        manifest_root: str | Path,
        media_root: str | Path,
        max_manifest_bytes: int = 10 * 1024 * 1024,
        max_media_bytes: int = 2 * 1024 * 1024 * 1024,
        max_items_per_scan: int = 10_000,
        max_queued_per_tenant: int = 10,
    ) -> None:
        self.database = Path(database)
        self.catalog = catalog
        self.manifest_root = Path(manifest_root).resolve()
        self.media_root = Path(media_root).resolve()
        self.max_manifest_bytes = max_manifest_bytes
        self.max_media_bytes = max_media_bytes
        self.max_items_per_scan = max_items_per_scan
        self.max_queued_per_tenant = max_queued_per_tenant
        if min(max_manifest_bytes, max_media_bytes, max_items_per_scan, max_queued_per_tenant) < 1:
            raise ValueError("Dataset Guard limits must be positive")
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
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
                CREATE TABLE IF NOT EXISTS dataset_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dataset_scans (
                    scan_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    manifest_uri TEXT NOT NULL,
                    manifest_sha256 TEXT NOT NULL,
                    catalog_ids_json TEXT NOT NULL,
                    policy_bundle_json TEXT NOT NULL,
                    detector_profile TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'queued', 'running', 'completed', 'completed_with_errors',
                        'cancelled', 'failed'
                    )),
                    total_items INTEGER NOT NULL,
                    processed_items INTEGER NOT NULL DEFAULT 0,
                    matched_items INTEGER NOT NULL DEFAULT 0,
                    review_items INTEGER NOT NULL DEFAULT 0,
                    error_items INTEGER NOT NULL DEFAULT 0,
                    cancellation_requested INTEGER NOT NULL DEFAULT 0
                        CHECK(cancellation_requested IN (0, 1)),
                    prior_scan_id TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(tenant_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_dataset_scans_claim
                ON dataset_scans(state, created_at);

                CREATE TABLE IF NOT EXISTS dataset_scan_items (
                    scan_id TEXT NOT NULL REFERENCES dataset_scans(scan_id) ON DELETE CASCADE,
                    tenant_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    item_id TEXT NOT NULL,
                    media_uri TEXT NOT NULL,
                    expected_sha256 TEXT,
                    intended_use TEXT,
                    state TEXT NOT NULL DEFAULT 'pending'
                        CHECK(state IN ('pending', 'completed', 'error')),
                    media_sha256 TEXT,
                    evidence_id TEXT,
                    matched INTEGER NOT NULL DEFAULT 0 CHECK(matched IN (0, 1)),
                    decision TEXT CHECK(decision IS NULL OR decision IN (
                        'allow', 'review', 'quarantine', 'block', 'not_evaluated'
                    )),
                    evidence_json TEXT,
                    error TEXT,
                    completed_at TEXT,
                    PRIMARY KEY(scan_id, ordinal),
                    UNIQUE(scan_id, item_id),
                    UNIQUE(evidence_id)
                );

                CREATE INDEX IF NOT EXISTS idx_dataset_scan_items_tenant
                ON dataset_scan_items(tenant_id, scan_id, ordinal);

                CREATE TABLE IF NOT EXISTS dataset_review_decisions (
                    decision_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    evidence_id TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK(outcome IN (
                        'allow', 'review', 'quarantine', 'block'
                    )),
                    reason TEXT NOT NULL,
                    reviewer TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(evidence_id) REFERENCES dataset_scan_items(evidence_id)
                        ON DELETE RESTRICT
                );

                CREATE INDEX IF NOT EXISTS idx_dataset_reviews_evidence
                ON dataset_review_decisions(tenant_id, evidence_id, created_at);

                CREATE TABLE IF NOT EXISTS dataset_audit_events (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    scan_id TEXT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO dataset_schema_meta(key, value)
                VALUES ('schema_version', ?)
                """,
                (str(DATASET_SCHEMA_VERSION),),
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(dataset_scan_items)")
            }
            if "matched" not in columns:
                connection.execute(
                    "ALTER TABLE dataset_scan_items ADD COLUMN matched INTEGER NOT NULL DEFAULT 0"
                )
            connection.commit()

    def recover_incomplete_scans(self) -> int:
        """Return interrupted work to the durable queue after process restart."""

        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE dataset_scans
                SET state = 'queued', started_at = NULL,
                    last_error = 'Worker interrupted; scan resumed from durable item state'
                WHERE state = 'running' AND cancellation_requested = 0
                """
            )
            connection.execute(
                """
                UPDATE dataset_scans
                SET state = 'cancelled', completed_at = ?
                WHERE state = 'running' AND cancellation_requested = 1
                """,
                (_utc_now(),),
            )
            connection.commit()
            return cursor.rowcount

    def create_scan(self, tenant_id: str, request: ScanRequest) -> tuple[dict[str, Any], bool]:
        """Validate and durably enqueue a manifest, honoring request idempotency."""

        self._validate_identifier(tenant_id, "tenant_id", 200)
        self._validate_identifier(request.idempotency_key, "idempotency_key", 200)
        if request.catalog_ids != ("default",):
            raise DatasetGuardError(
                "The reference service currently supports catalog_ids=['default']"
            )
        if request.detector_profile != "constellation-v1":
            raise DatasetGuardError("The only supported detector_profile is 'constellation-v1'")
        if not request.manifest_uri or len(request.manifest_uri) > 4_096:
            raise DatasetGuardError("manifest_uri must contain 1 to 4096 characters")
        manifest_digest = self._validate_sha256(request.manifest_sha256, "manifest_sha256")
        self._validate_policy_bundle(request.policy_bundle)
        try:
            policy_json = _canonical_json(request.policy_bundle)
        except (TypeError, ValueError) as exc:
            raise DatasetGuardError("policy_bundle must be JSON serializable") from exc
        if len(policy_json.encode()) > 64 * 1024:
            raise DatasetGuardQuotaExceeded("policy_bundle exceeds the 64 KiB limit")
        request_payload = {
            "manifest_uri": request.manifest_uri,
            "manifest_sha256": manifest_digest,
            "catalog_ids": request.catalog_ids,
            "policy_bundle": request.policy_bundle,
            "detector_profile": request.detector_profile,
            "prior_scan_id": request.prior_scan_id,
        }
        request_sha256 = hashlib.sha256(_canonical_json(request_payload).encode()).hexdigest()

        with self._connection() as connection:
            existing = connection.execute(
                "SELECT * FROM dataset_scans WHERE tenant_id = ? AND idempotency_key = ?",
                (tenant_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    raise DatasetGuardConflict(
                        "The idempotency key is already bound to a different request"
                    )
                return self._scan_from_row(existing), False
            active = connection.execute(
                """
                SELECT COUNT(*) FROM dataset_scans
                WHERE tenant_id = ? AND state IN ('queued', 'running')
                """,
                (tenant_id,),
            ).fetchone()[0]
            if int(active) >= self.max_queued_per_tenant:
                raise DatasetGuardQuotaExceeded("The tenant scan queue is at its configured limit")
            if request.prior_scan_id is not None:
                prior = connection.execute(
                    "SELECT 1 FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?",
                    (tenant_id, request.prior_scan_id),
                ).fetchone()
                if prior is None:
                    raise DatasetGuardNotFound("Prior scan not found")

        items = self._load_manifest(request.manifest_uri, manifest_digest)
        scan_id = str(uuid.uuid4())
        created_at = _utc_now()
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM dataset_scans WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    if existing["request_sha256"] == request_sha256:
                        return self._scan_from_row(existing), False
                    raise DatasetGuardConflict(
                        "The idempotency key is already bound to a different request"
                    )
                active = connection.execute(
                    """
                    SELECT COUNT(*) FROM dataset_scans
                    WHERE tenant_id = ? AND state IN ('queued', 'running')
                    """,
                    (tenant_id,),
                ).fetchone()[0]
                if int(active) >= self.max_queued_per_tenant:
                    raise DatasetGuardQuotaExceeded(
                        "The tenant scan queue is at its configured limit"
                    )
                connection.execute(
                    """
                    INSERT INTO dataset_scans(
                        scan_id, tenant_id, idempotency_key, request_sha256,
                        manifest_uri, manifest_sha256, catalog_ids_json,
                        policy_bundle_json, detector_profile, state, total_items,
                        prior_scan_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?)
                    """,
                    (
                        scan_id,
                        tenant_id,
                        request.idempotency_key,
                        request_sha256,
                        request.manifest_uri,
                        manifest_digest,
                        _canonical_json(request.catalog_ids),
                        policy_json,
                        request.detector_profile,
                        len(items),
                        request.prior_scan_id,
                        created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO dataset_scan_items(
                        scan_id, tenant_id, ordinal, item_id, media_uri,
                        expected_sha256, intended_use
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        (
                            scan_id,
                            tenant_id,
                            item.ordinal,
                            item.item_id,
                            item.media_uri,
                            item.media_sha256,
                            item.intended_use,
                        )
                        for item in items
                    ),
                )
                self._audit(
                    connection,
                    tenant_id,
                    scan_id,
                    "scan.created",
                    {"manifest_sha256": manifest_digest, "total_items": len(items)},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                existing = connection.execute(
                    "SELECT * FROM dataset_scans WHERE tenant_id = ? AND idempotency_key = ?",
                    (tenant_id, request.idempotency_key),
                ).fetchone()
                if existing is not None and existing["request_sha256"] == request_sha256:
                    return self._scan_from_row(existing), False
                raise DatasetGuardConflict("Could not create an idempotent scan") from exc
        return self.get_scan(tenant_id, scan_id), True

    def get_scan(self, tenant_id: str, scan_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?",
                (tenant_id, scan_id),
            ).fetchone()
            if row is None:
                raise DatasetGuardNotFound("Scan not found")
            return self._scan_from_row(row)

    def list_items(
        self, tenant_id: str, scan_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        self.get_scan(tenant_id, scan_id)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM dataset_scan_items
                WHERE tenant_id = ? AND scan_id = ?
                ORDER BY ordinal LIMIT ? OFFSET ?
                """,
                (tenant_id, scan_id, min(max(limit, 1), 500), max(offset, 0)),
            ).fetchall()
            return [self._item_from_row(connection, row) for row in rows]

    def request_cancellation(self, tenant_id: str, scan_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?",
                (tenant_id, scan_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DatasetGuardNotFound("Scan not found")
            if row["state"] not in TERMINAL_STATES:
                now = _utc_now()
                state = "cancelled" if row["state"] == "queued" else row["state"]
                completed_at = now if state == "cancelled" else row["completed_at"]
                connection.execute(
                    """
                    UPDATE dataset_scans
                    SET cancellation_requested = 1, state = ?, completed_at = ?
                    WHERE tenant_id = ? AND scan_id = ?
                    """,
                    (state, completed_at, tenant_id, scan_id),
                )
                self._audit(connection, tenant_id, scan_id, "scan.cancellation_requested", {})
            connection.commit()
        return self.get_scan(tenant_id, scan_id)

    def add_review_decision(
        self,
        tenant_id: str,
        evidence_id: str,
        *,
        outcome: Decision,
        reason: str,
        reviewer: str | None,
    ) -> dict[str, Any]:
        if outcome not in {"allow", "review", "quarantine", "block"}:
            raise DatasetGuardError(
                "Human review outcome must be allow, review, quarantine, or block"
            )
        self._validate_identifier(reason, "reason", 4_096)
        if reviewer is not None:
            self._validate_identifier(reviewer, "reviewer", 200)
        decision_id = str(uuid.uuid4())
        created_at = _utc_now()
        with self._connection() as connection:
            item = connection.execute(
                """
                SELECT scan_id FROM dataset_scan_items
                WHERE tenant_id = ? AND evidence_id = ?
                """,
                (tenant_id, evidence_id),
            ).fetchone()
            if item is None:
                raise DatasetGuardNotFound("Evidence not found")
            connection.execute(
                """
                INSERT INTO dataset_review_decisions(
                    decision_id, tenant_id, evidence_id, outcome, reason, reviewer, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (decision_id, tenant_id, evidence_id, outcome, reason, reviewer, created_at),
            )
            self._audit(
                connection,
                tenant_id,
                str(item["scan_id"]),
                "review.decision_recorded",
                {"decision_id": decision_id, "evidence_id": evidence_id, "outcome": outcome},
            )
            connection.commit()
        return {
            "decision_id": decision_id,
            "evidence_id": evidence_id,
            "outcome": outcome,
            "reason": reason,
            "reviewer": reviewer,
            "created_at": created_at,
        }

    def export_jsonl(self, tenant_id: str, scan_id: str) -> str:
        scan = self.get_scan(tenant_id, scan_id)
        if scan["state"] not in TERMINAL_STATES:
            raise DatasetGuardConflict(
                "A scan can be exported only after it reaches a terminal state"
            )
        records: list[dict[str, Any]] = [
            {
                "schema_version": "auxris.dataset-scan.v1",
                "record_type": "scan",
                **scan,
            }
        ]
        for item in self.list_items(tenant_id, scan_id, limit=self.max_items_per_scan):
            records.append(
                {
                    "schema_version": "auxris.dataset-scan-item.v1",
                    "record_type": "item",
                    **item,
                }
            )
        return "".join(_canonical_json(record) + "\n" for record in records)

    def run_next_scan(self) -> bool:
        """Claim and execute at most one queued scan; safe to call from one local worker."""

        claimed = self._claim_next_scan()
        if claimed is None:
            return False
        scan_id, tenant_id = claimed
        self._run_scan(tenant_id, scan_id)
        return True

    def _claim_next_scan(self) -> tuple[str, str] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT scan_id, tenant_id FROM dataset_scans
                WHERE state = 'queued' AND cancellation_requested = 0
                ORDER BY created_at, scan_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE dataset_scans SET state = 'running', started_at = ?, last_error = NULL
                WHERE scan_id = ? AND tenant_id = ? AND state = 'queued'
                """,
                (now, row["scan_id"], row["tenant_id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            self._audit(
                connection,
                str(row["tenant_id"]),
                str(row["scan_id"]),
                "scan.started",
                {},
            )
            connection.commit()
            return str(row["scan_id"]), str(row["tenant_id"])

    def _run_scan(self, tenant_id: str, scan_id: str) -> None:
        try:
            with self._connection() as connection:
                scan = connection.execute(
                    "SELECT * FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?",
                    (tenant_id, scan_id),
                ).fetchone()
                if scan is None:
                    return
                policy_bundle = dict(json.loads(scan["policy_bundle_json"]))
                pending = connection.execute(
                    """
                    SELECT * FROM dataset_scan_items
                    WHERE tenant_id = ? AND scan_id = ? AND state = 'pending'
                    ORDER BY ordinal
                    """,
                    (tenant_id, scan_id),
                ).fetchall()
            for row in pending:
                if self._cancellation_requested(tenant_id, scan_id):
                    self._finish_scan(tenant_id, scan_id, "cancelled")
                    return
                self._process_item(tenant_id, scan_id, row, policy_bundle)
            state: ScanState = (
                "completed_with_errors" if self._error_count(tenant_id, scan_id) else "completed"
            )
            self._finish_scan(tenant_id, scan_id, state)
        except Exception as exc:
            self._fail_scan(tenant_id, scan_id, exc)

    def _process_item(
        self,
        tenant_id: str,
        scan_id: str,
        row: sqlite3.Row,
        policy_bundle: Mapping[str, Any],
    ) -> None:
        try:
            media_path = self._resolve_file_uri(str(row["media_uri"]), self.media_root, "media")
            if media_path.stat().st_size > self.max_media_bytes:
                raise DatasetGuardQuotaExceeded("Media exceeds the configured byte limit")
            media_sha256 = self._sha256_file(media_path)
            expected = row["expected_sha256"]
            if expected is not None and not hmac.compare_digest(media_sha256, str(expected)):
                raise DatasetGuardError("Media SHA-256 does not match the manifest")
            intended_use = row["intended_use"] or policy_bundle.get("intended_use")
            if intended_use is not None and not isinstance(intended_use, str):
                raise DatasetGuardError("policy_bundle.intended_use must be a string")
            matches = self.catalog.identify_path(media_path, intended_use=intended_use)
            decision, reason_codes = self._decision_for_matches(matches)
            evidence_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, f"auxris:{scan_id}:{row['item_id']}:{media_sha256}")
            )
            config_sha256 = hashlib.sha256(
                _canonical_json(
                    {
                        "algorithm": self.catalog.fingerprinter.algorithm,
                        "policy_bundle": policy_bundle,
                    }
                ).encode()
            ).hexdigest()
            evidence: dict[str, Any] = {
                "schema_version": "auxris.evidence.v1",
                "evidence_id": evidence_id,
                "scan_id": scan_id,
                "created_at": _utc_now(),
                "subject": {
                    "kind": "dataset_asset",
                    "sha256": media_sha256,
                    "media_type": "audio",
                    "external_id": str(row["item_id"]),
                },
                "detector": {
                    "id": "auxris.recording-segment-match",
                    "version": __version__,
                    "config_sha256": config_sha256,
                    "execution_mode": "embedded",
                },
                "findings": [self._finding(match) for match in matches],
                "policy": {
                    "decision": decision,
                    "policy_id": str(policy_bundle.get("policy_id", "auxris.default-review")),
                    "policy_revision": str(policy_bundle.get("revision", "1")),
                    "reason_codes": reason_codes,
                    "explanation": self._decision_explanation(decision, bool(matches)),
                },
                "limitations": [
                    "Evidence is limited to the operator-controlled default catalog.",
                    "No match does not establish that an item is unprotected or safe to use.",
                    (
                        "The detector does not make copyright ownership or "
                        "infringement determinations."
                    ),
                ],
            }
            evidence["integrity"] = {
                "payload_sha256": hashlib.sha256(_canonical_json(evidence).encode()).hexdigest()
            }
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE dataset_scan_items
                    SET state = 'completed', media_sha256 = ?, evidence_id = ?, matched = ?,
                        decision = ?, evidence_json = ?, error = NULL, completed_at = ?
                    WHERE tenant_id = ? AND scan_id = ? AND ordinal = ? AND state = 'pending'
                    """,
                    (
                        media_sha256,
                        evidence_id,
                        int(bool(matches)),
                        decision,
                        _canonical_json(evidence),
                        _utc_now(),
                        tenant_id,
                        scan_id,
                        row["ordinal"],
                    ),
                )
                if cursor.rowcount == 1:
                    self._refresh_counts(connection, tenant_id, scan_id)
                    self._audit(
                        connection,
                        tenant_id,
                        scan_id,
                        "scan.item_completed",
                        {
                            "item_id": row["item_id"],
                            "evidence_id": evidence_id,
                            "decision": decision,
                        },
                    )
                connection.commit()
        except (AudioDecodeError, CatalogError, DatasetGuardError, OSError) as exc:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE dataset_scan_items
                    SET state = 'error', error = ?, completed_at = ?
                    WHERE tenant_id = ? AND scan_id = ? AND ordinal = ? AND state = 'pending'
                    """,
                    (str(exc)[:4_096], _utc_now(), tenant_id, scan_id, row["ordinal"]),
                )
                if cursor.rowcount == 1:
                    self._refresh_counts(connection, tenant_id, scan_id)
                    self._audit(
                        connection,
                        tenant_id,
                        scan_id,
                        "scan.item_error",
                        {"item_id": row["item_id"], "error": str(exc)[:1_024]},
                    )
                connection.commit()

    @staticmethod
    def _finding(match: Any) -> dict[str, Any]:
        return {
            "type": "recording_segment_match",
            "catalog_id": "default",
            "asset_id": match.asset.asset_id,
            "ranking_score": match.score,
            "aligned_votes": match.evidence.aligned_votes,
            "query_fingerprints": match.evidence.query_fingerprints,
            "coverage": match.evidence.coverage,
            "offset_frames": match.evidence.offset_frames,
            "query_range": {
                "start_seconds": match.evidence.query_start_seconds,
                "end_seconds": match.evidence.query_end_seconds,
            },
            "source_range": {
                "start_seconds": match.evidence.source_start_seconds,
                "end_seconds": match.evidence.source_end_seconds,
            },
        }

    @staticmethod
    def _decision_for_matches(matches: Sequence[Any]) -> tuple[Decision, list[str]]:
        if not matches:
            return "review", ["NO_CATALOG_MATCH"]
        policies = {match.policy.decision for match in matches}
        if "not_allowed" in policies:
            return "quarantine", ["CATALOG_POLICY_NOT_ALLOWED"]
        if policies & {"review_required", "not_evaluated"}:
            return "review", ["CATALOG_POLICY_REQUIRES_REVIEW"]
        return "allow", ["CATALOG_POLICY_ALLOWS_REQUESTED_USE"]

    @staticmethod
    def _decision_explanation(decision: Decision, matched: bool) -> str:
        if not matched:
            return "No catalog match was found; human review remains required."
        return f"Operator catalog evidence produced the '{decision}' workflow decision."

    def _finish_scan(self, tenant_id: str, scan_id: str, state: ScanState) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._refresh_counts(connection, tenant_id, scan_id)
            connection.execute(
                """
                UPDATE dataset_scans SET state = ?, completed_at = ?
                WHERE tenant_id = ? AND scan_id = ?
                """,
                (state, _utc_now(), tenant_id, scan_id),
            )
            self._audit(connection, tenant_id, scan_id, f"scan.{state}", {})
            connection.commit()

    def _fail_scan(self, tenant_id: str, scan_id: str, exc: Exception) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dataset_scans
                SET state = 'failed', last_error = ?, completed_at = ?
                WHERE tenant_id = ? AND scan_id = ?
                """,
                (str(exc)[:4_096], _utc_now(), tenant_id, scan_id),
            )
            self._audit(
                connection,
                tenant_id,
                scan_id,
                "scan.failed",
                {"error": str(exc)[:1_024]},
            )
            connection.commit()

    def _refresh_counts(self, connection: sqlite3.Connection, tenant_id: str, scan_id: str) -> None:
        counts = connection.execute(
            """
            SELECT
                SUM(CASE WHEN state IN ('completed', 'error') THEN 1 ELSE 0 END) AS processed,
                SUM(matched) AS matched,
                SUM(
                    CASE WHEN decision IN ('review', 'quarantine', 'block') THEN 1 ELSE 0 END
                ) AS review_count,
                SUM(CASE WHEN state = 'error' THEN 1 ELSE 0 END) AS errors
            FROM dataset_scan_items WHERE tenant_id = ? AND scan_id = ?
            """,
            (tenant_id, scan_id),
        ).fetchone()
        connection.execute(
            """
            UPDATE dataset_scans
            SET processed_items = ?, matched_items = ?, review_items = ?, error_items = ?
            WHERE tenant_id = ? AND scan_id = ?
            """,
            (
                int(counts["processed"] or 0),
                int(counts["matched"] or 0),
                int(counts["review_count"] or 0),
                int(counts["errors"] or 0),
                tenant_id,
                scan_id,
            ),
        )

    def _error_count(self, tenant_id: str, scan_id: str) -> int:
        with self._connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM dataset_scan_items
                    WHERE tenant_id = ? AND scan_id = ? AND state = 'error'
                    """,
                    (tenant_id, scan_id),
                ).fetchone()[0]
            )

    def _cancellation_requested(self, tenant_id: str, scan_id: str) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT cancellation_requested FROM dataset_scans
                WHERE tenant_id = ? AND scan_id = ?
                """,
                (tenant_id, scan_id),
            ).fetchone()
            return row is None or bool(row[0])

    def _load_manifest(self, uri: str, expected_sha256: str) -> list[ManifestItem]:
        path = self._resolve_file_uri(uri, self.manifest_root, "manifest")
        try:
            with path.open("rb") as handle:
                payload = handle.read(self.max_manifest_bytes + 1)
        except OSError as exc:
            raise DatasetGuardError("The manifest URI is not readable") from exc
        if len(payload) > self.max_manifest_bytes:
            raise DatasetGuardQuotaExceeded("Manifest exceeds the configured byte limit")
        actual_sha256 = hashlib.sha256(payload).hexdigest()
        if not hmac.compare_digest(actual_sha256, expected_sha256):
            raise DatasetGuardError("Manifest SHA-256 does not match manifest_sha256")
        items: list[ManifestItem] = []
        seen: set[str] = set()
        for line_number, raw_line in enumerate(payload.splitlines(), start=1):
            if not raw_line.strip():
                continue
            if len(items) >= self.max_items_per_scan:
                raise DatasetGuardQuotaExceeded("Manifest exceeds the configured item limit")
            try:
                value = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise DatasetGuardError(f"Manifest line {line_number} is not valid JSON") from exc
            if not isinstance(value, dict):
                raise DatasetGuardError(f"Manifest line {line_number} must be an object")
            allowed = {"item_id", "media_uri", "media_sha256", "intended_use"}
            unexpected = set(value) - allowed
            if unexpected:
                raise DatasetGuardError(
                    f"Manifest line {line_number} has unsupported fields: {sorted(unexpected)}"
                )
            item_id = value.get("item_id")
            media_uri = value.get("media_uri")
            if not isinstance(item_id, str) or not item_id or len(item_id) > 500:
                raise DatasetGuardError(f"Manifest line {line_number} has an invalid item_id")
            if item_id in seen:
                raise DatasetGuardError(f"Manifest item_id is duplicated: {item_id}")
            if not isinstance(media_uri, str) or not media_uri or len(media_uri) > 4_096:
                raise DatasetGuardError(f"Manifest line {line_number} has an invalid media_uri")
            self._resolve_file_uri(media_uri, self.media_root, "media")
            media_sha = value.get("media_sha256")
            if media_sha is not None:
                if not isinstance(media_sha, str):
                    raise DatasetGuardError(
                        f"Manifest line {line_number} has an invalid media_sha256"
                    )
                media_sha = self._validate_sha256(media_sha, "media_sha256")
            intended_use = value.get("intended_use")
            if intended_use is not None and (
                not isinstance(intended_use, str) or not intended_use or len(intended_use) > 200
            ):
                raise DatasetGuardError(f"Manifest line {line_number} has an invalid intended_use")
            seen.add(item_id)
            items.append(
                ManifestItem(
                    ordinal=len(items),
                    item_id=item_id,
                    media_uri=media_uri,
                    media_sha256=media_sha,
                    intended_use=intended_use,
                )
            )
        if not items:
            raise DatasetGuardError("Manifest must contain at least one item")
        return items

    @staticmethod
    def _resolve_file_uri(uri: str, root: Path, kind: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise DatasetGuardError(f"Only local file:// {kind} URIs are supported")
        if parsed.query or parsed.fragment:
            raise DatasetGuardError(f"The {kind} URI cannot contain a query or fragment")
        try:
            candidate = Path(unquote(parsed.path)).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DatasetGuardError(f"The {kind} URI does not resolve to a readable file") from exc
        if not candidate.is_file() or not candidate.is_relative_to(root):
            raise DatasetGuardError(f"The {kind} URI is outside its configured root")
        return candidate

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                total += len(chunk)
                if total > self.max_media_bytes:
                    raise DatasetGuardQuotaExceeded("Media exceeds the configured byte limit")
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_sha256(value: str, name: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise DatasetGuardError(f"{name} must be a lowercase hexadecimal SHA-256 digest")
        return value

    @staticmethod
    def _validate_identifier(value: str, name: str, maximum: int) -> None:
        if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
            raise DatasetGuardError(f"{name} must contain 1 to {maximum} printable characters")

    @classmethod
    def _validate_policy_bundle(cls, bundle: Mapping[str, Any]) -> None:
        allowed = {"policy_id", "revision", "intended_use"}
        unexpected = set(bundle) - allowed
        if unexpected:
            raise DatasetGuardError(f"policy_bundle has unsupported fields: {sorted(unexpected)}")
        for field, maximum in (("policy_id", 200), ("revision", 200), ("intended_use", 200)):
            value = bundle.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise DatasetGuardError(f"policy_bundle.{field} must be a string")
                cls._validate_identifier(value, f"policy_bundle.{field}", maximum)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        tenant_id: str,
        scan_id: str | None,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO dataset_audit_events(
                event_id, tenant_id, scan_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                scan_id,
                event_type,
                _canonical_json(payload),
                _utc_now(),
            ),
        )

    @staticmethod
    def _scan_from_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "scan_id": str(row["scan_id"]),
            "state": str(row["state"]),
            "manifest_uri": str(row["manifest_uri"]),
            "manifest_sha256": str(row["manifest_sha256"]),
            "catalog_ids": list(json.loads(row["catalog_ids_json"])),
            "policy_bundle": dict(json.loads(row["policy_bundle_json"])),
            "detector_profile": str(row["detector_profile"]),
            "total_items": int(row["total_items"]),
            "processed_items": int(row["processed_items"]),
            "matched_items": int(row["matched_items"]),
            "review_items": int(row["review_items"]),
            "error_items": int(row["error_items"]),
            "cancellation_requested": bool(row["cancellation_requested"]),
            "prior_scan_id": row["prior_scan_id"],
            "last_error": row["last_error"],
            "created_at": str(row["created_at"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _item_from_row(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        evidence = json.loads(row["evidence_json"]) if row["evidence_json"] else None
        reviews = connection.execute(
            """
            SELECT decision_id, outcome, reason, reviewer, created_at
            FROM dataset_review_decisions
            WHERE tenant_id = ? AND evidence_id = ? ORDER BY created_at, decision_id
            """,
            (row["tenant_id"], row["evidence_id"]),
        ).fetchall()
        return {
            "ordinal": int(row["ordinal"]),
            "item_id": str(row["item_id"]),
            "media_uri": str(row["media_uri"]),
            "expected_sha256": row["expected_sha256"],
            "media_sha256": row["media_sha256"],
            "state": str(row["state"]),
            "evidence_id": row["evidence_id"],
            "matched": bool(row["matched"]),
            "decision": row["decision"],
            "evidence": evidence,
            "error": row["error"],
            "completed_at": row["completed_at"],
            "review_decisions": [dict(review) for review in reviews],
        }
