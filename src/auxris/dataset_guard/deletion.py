"""Durable deletion lifecycle for Dataset Guard evidence and tenant catalog assets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from auxris.core.tenant_catalog import CatalogProvider

DeletionTarget = Literal["scan", "catalog_asset"]
DeletionState = Literal["queued", "running", "completed", "failed"]


class DeletionError(RuntimeError):
    """Base error for invalid deletion operations."""


class DeletionNotFound(DeletionError):
    """Raised when a tenant cannot access a deletion target or job."""


class DeletionConflict(DeletionError):
    """Raised when deletion state or idempotency prevents an operation."""


class DeletionQuotaExceeded(DeletionError):
    """Raised when a tenant's active deletion queue is full."""


@dataclass(frozen=True, slots=True)
class DeletionRequest:
    target_type: DeletionTarget
    target_id: str
    idempotency_key: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DeletionManager:
    """Queue and execute logical deletion jobs with digest-only tombstones."""

    def __init__(
        self,
        database: str | Path,
        catalog_provider: CatalogProvider,
        *,
        max_queued_per_tenant: int = 100,
    ) -> None:
        self.database = Path(database)
        self.catalog_provider = catalog_provider
        self.max_queued_per_tenant = max_queued_per_tenant
        if max_queued_per_tenant < 1:
            raise ValueError("max_queued_per_tenant must be positive")
        self.initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA secure_delete = ON")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS dataset_deletion_schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS dataset_deletion_jobs (
                    deletion_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL,
                    target_type TEXT NOT NULL CHECK(target_type IN ('scan', 'catalog_asset')),
                    target_id TEXT,
                    target_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(
                        state IN ('queued', 'running', 'completed', 'failed')
                    ),
                    disposition TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(tenant_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_dataset_deletions_claim
                ON dataset_deletion_jobs(state, created_at);

                CREATE TABLE IF NOT EXISTS dataset_deletion_audit (
                    event_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    deletion_id TEXT NOT NULL REFERENCES dataset_deletion_jobs(deletion_id)
                        ON DELETE RESTRICT,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO dataset_deletion_schema_meta(key, value)
                VALUES ('schema_version', '1')
                """
            )
            connection.commit()

    def recover_incomplete_jobs(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE dataset_deletion_jobs
                SET state = 'queued', started_at = NULL,
                    last_error = 'Worker interrupted; deletion returned to the durable queue'
                WHERE state = 'running'
                """
            )
            connection.commit()
            return cursor.rowcount

    def create_deletion(
        self, tenant_id: str, request: DeletionRequest
    ) -> tuple[dict[str, object], bool]:
        self._validate_identifier(tenant_id, "tenant_id", 200)
        self._validate_identifier(request.idempotency_key, "idempotency_key", 200)
        self._validate_identifier(request.target_id, "target_id", 500)
        if request.target_type not in {"scan", "catalog_asset"}:
            raise DeletionError("target_type must be scan or catalog_asset")
        request_sha256 = hashlib.sha256(
            _canonical_json(
                {"target_type": request.target_type, "target_id": request.target_id}
            ).encode()
        ).hexdigest()
        target_sha256 = hashlib.sha256(request.target_id.encode()).hexdigest()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM dataset_deletion_jobs
                WHERE tenant_id = ? AND idempotency_key = ?
                """,
                (tenant_id, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                if existing["request_sha256"] != request_sha256:
                    connection.rollback()
                    raise DeletionConflict(
                        "The idempotency key is already bound to another deletion request"
                    )
                connection.commit()
                return self._job_from_row(existing), False
            active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM dataset_deletion_jobs
                    WHERE tenant_id = ? AND state IN ('queued', 'running')
                    """,
                    (tenant_id,),
                ).fetchone()[0]
            )
            if active >= self.max_queued_per_tenant:
                connection.rollback()
                raise DeletionQuotaExceeded("The tenant deletion queue is at its configured limit")
            self._validate_target(connection, tenant_id, request)
            deletion_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO dataset_deletion_jobs(
                    deletion_id, tenant_id, idempotency_key, request_sha256,
                    target_type, target_id, target_sha256, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'queued', ?)
                """,
                (
                    deletion_id,
                    tenant_id,
                    request.idempotency_key,
                    request_sha256,
                    request.target_type,
                    request.target_id,
                    target_sha256,
                    _utc_now(),
                ),
            )
            self._audit(
                connection,
                tenant_id,
                deletion_id,
                "deletion.created",
                {"target_type": request.target_type, "target_sha256": target_sha256},
            )
            connection.commit()
        return self.get_deletion(tenant_id, deletion_id), True

    def retry_deletion(self, tenant_id: str, deletion_id: str) -> dict[str, object]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM dataset_deletion_jobs
                WHERE tenant_id = ? AND deletion_id = ?
                """,
                (tenant_id, deletion_id),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise DeletionNotFound("Deletion job not found")
            if row["state"] != "failed":
                connection.rollback()
                raise DeletionConflict("Only a failed deletion can be retried")
            if row["target_id"] is None:
                connection.rollback()
                raise DeletionConflict("A redacted deletion target cannot be retried")
            active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM dataset_deletion_jobs
                    WHERE tenant_id = ? AND state IN ('queued', 'running')
                    """,
                    (tenant_id,),
                ).fetchone()[0]
            )
            if active >= self.max_queued_per_tenant:
                connection.rollback()
                raise DeletionQuotaExceeded("The tenant deletion queue is at its configured limit")
            connection.execute(
                """
                UPDATE dataset_deletion_jobs
                SET state = 'queued', started_at = NULL, completed_at = NULL, last_error = NULL
                WHERE tenant_id = ? AND deletion_id = ? AND state = 'failed'
                """,
                (tenant_id, deletion_id),
            )
            self._audit(connection, tenant_id, deletion_id, "deletion.retry_requested", {})
            connection.commit()
        return self.get_deletion(tenant_id, deletion_id)

    def get_deletion(self, tenant_id: str, deletion_id: str) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dataset_deletion_jobs
                WHERE tenant_id = ? AND deletion_id = ?
                """,
                (tenant_id, deletion_id),
            ).fetchone()
            if row is None:
                raise DeletionNotFound("Deletion job not found")
            return self._job_from_row(row)

    def run_next_deletion(self) -> bool:
        claimed = self._claim_next()
        if claimed is None:
            return False
        deletion_id, tenant_id = claimed
        try:
            self._execute(tenant_id, deletion_id)
        except Exception as exc:
            self._fail(tenant_id, deletion_id, exc)
        return True

    def _validate_target(
        self, connection: sqlite3.Connection, tenant_id: str, request: DeletionRequest
    ) -> None:
        if request.target_type == "scan":
            row = connection.execute(
                """
                SELECT state FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?
                """,
                (tenant_id, request.target_id),
            ).fetchone()
            if row is None:
                raise DeletionNotFound("Deletion target not found")
            if row["state"] not in {
                "completed",
                "completed_with_errors",
                "cancelled",
                "failed",
            }:
                raise DeletionConflict("A scan must be terminal before it can be deleted")
            return
        catalog = self.catalog_provider.get_catalog(tenant_id)
        if catalog.get_asset(request.target_id) is None:
            raise DeletionNotFound("Deletion target not found")

    def _claim_next(self) -> tuple[str, str] | None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT deletion_id, tenant_id FROM dataset_deletion_jobs
                WHERE state = 'queued' ORDER BY created_at, deletion_id LIMIT 1
                """
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE dataset_deletion_jobs
                SET state = 'running', started_at = ?, last_error = NULL
                WHERE deletion_id = ? AND tenant_id = ? AND state = 'queued'
                """,
                (_utc_now(), row["deletion_id"], row["tenant_id"]),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            self._audit(
                connection,
                str(row["tenant_id"]),
                str(row["deletion_id"]),
                "deletion.started",
                {},
            )
            connection.commit()
            return str(row["deletion_id"]), str(row["tenant_id"])

    def _execute(self, tenant_id: str, deletion_id: str) -> None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM dataset_deletion_jobs
                WHERE tenant_id = ? AND deletion_id = ? AND state = 'running'
                """,
                (tenant_id, deletion_id),
            ).fetchone()
            if row is None:
                raise DeletionNotFound("Claimed deletion job not found")
            target_type = str(row["target_type"])
            target_id = row["target_id"]
            if target_id is None:
                raise DeletionConflict("Deletion target was already redacted")

        if target_type == "scan":
            disposition = self._delete_scan(tenant_id, str(target_id))
        elif target_type == "catalog_asset":
            removed = self.catalog_provider.get_catalog(tenant_id).remove_asset(str(target_id))
            disposition = "logical_delete" if removed else "already_absent"
        else:
            raise DeletionError("Deletion job has an unsupported target type")

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dataset_deletion_jobs
                SET state = 'completed', target_id = NULL, disposition = ?, completed_at = ?
                WHERE tenant_id = ? AND deletion_id = ? AND state = 'running'
                """,
                (disposition, _utc_now(), tenant_id, deletion_id),
            )
            self._audit(
                connection,
                tenant_id,
                deletion_id,
                "deletion.completed",
                {"disposition": disposition, "target_sha256": row["target_sha256"]},
            )
            connection.commit()
            with suppress(sqlite3.Error):
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _delete_scan(self, tenant_id: str, scan_id: str) -> str:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            evidence = connection.execute(
                """
                SELECT evidence_id FROM dataset_scan_items
                WHERE tenant_id = ? AND scan_id = ? AND evidence_id IS NOT NULL
                """,
                (tenant_id, scan_id),
            ).fetchall()
            evidence_ids = [str(row["evidence_id"]) for row in evidence]
            for start in range(0, len(evidence_ids), 800):
                chunk = evidence_ids[start : start + 800]
                placeholders = ",".join("?" for _ in chunk)
                connection.execute(
                    f"DELETE FROM dataset_review_decisions WHERE tenant_id = ? "  # noqa: S608
                    f"AND evidence_id IN ({placeholders})",
                    (tenant_id, *chunk),
                )
            connection.execute(
                "DELETE FROM dataset_audit_events WHERE tenant_id = ? AND scan_id = ?",
                (tenant_id, scan_id),
            )
            cursor = connection.execute(
                "DELETE FROM dataset_scans WHERE tenant_id = ? AND scan_id = ?",
                (tenant_id, scan_id),
            )
            connection.commit()
            return "logical_delete" if cursor.rowcount else "already_absent"

    def _fail(self, tenant_id: str, deletion_id: str, exc: Exception) -> None:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE dataset_deletion_jobs
                SET state = 'failed', last_error = ?, completed_at = ?
                WHERE tenant_id = ? AND deletion_id = ?
                """,
                (str(exc)[:4_096], _utc_now(), tenant_id, deletion_id),
            )
            self._audit(
                connection,
                tenant_id,
                deletion_id,
                "deletion.failed",
                {"error_type": type(exc).__name__},
            )
            connection.commit()

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        tenant_id: str,
        deletion_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> None:
        connection.execute(
            """
            INSERT INTO dataset_deletion_audit(
                event_id, tenant_id, deletion_id, event_type, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()),
                tenant_id,
                deletion_id,
                event_type,
                _canonical_json(payload),
                _utc_now(),
            ),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> dict[str, object]:
        return {
            "deletion_id": str(row["deletion_id"]),
            "target_type": str(row["target_type"]),
            "target_id": row["target_id"],
            "target_sha256": str(row["target_sha256"]),
            "state": str(row["state"]),
            "disposition": row["disposition"],
            "last_error": row["last_error"],
            "created_at": str(row["created_at"]),
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
        }

    @staticmethod
    def _validate_identifier(value: str, name: str, maximum: int) -> None:
        if not value or len(value) > maximum or any(ord(character) < 32 for character in value):
            raise DeletionError(f"{name} must contain 1 to {maximum} printable characters")
