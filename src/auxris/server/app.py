"""Authenticated HTTP adapter around the deterministic Auxris core."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal, cast

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi import Path as ApiPath
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from auxris import __version__
from auxris.core.index import Catalog, CatalogError
from auxris.core.models import Asset
from auxris.core.tenant_catalog import TenantCatalogRegistry
from auxris.dataset_guard import (
    DatasetGuard,
    DatasetGuardConflict,
    DatasetGuardError,
    DatasetGuardNotFound,
    DatasetGuardQuotaExceeded,
    ScanRequest,
)
from auxris.dataset_guard.deletion import (
    DeletionConflict,
    DeletionError,
    DeletionManager,
    DeletionNotFound,
    DeletionQuotaExceeded,
    DeletionRequest,
)
from auxris.dataset_guard.service import Decision

STATIC_DIR = Path(__file__).parent / "static"
LOGGER = logging.getLogger(__name__)


class Settings:
    def __init__(self) -> None:
        self.database = Path(os.getenv("AUXRIS_DB_PATH", "./data/auxris.sqlite3")).resolve()
        self.api_key = os.getenv("AUXRIS_API_KEY", "")
        self.tenant_id = os.getenv("AUXRIS_TENANT_ID", "default")
        self.api_keys = self._api_keys()
        self.max_upload_bytes = int(os.getenv("AUXRIS_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
        self.manifest_root = Path(
            os.getenv("AUXRIS_MANIFEST_ROOT", str(self.database.parent / "manifests"))
        ).resolve()
        self.media_root = Path(
            os.getenv("AUXRIS_MEDIA_ROOT", str(self.database.parent / "media"))
        ).resolve()
        self.tenant_catalog_root = Path(
            os.getenv("AUXRIS_TENANT_CATALOG_ROOT", str(self.database.parent / "tenant-catalogs"))
        ).resolve()
        self.max_manifest_bytes = int(os.getenv("AUXRIS_MAX_MANIFEST_BYTES", str(10 * 1024 * 1024)))
        self.max_media_bytes = int(os.getenv("AUXRIS_MAX_MEDIA_BYTES", str(2 * 1024 * 1024 * 1024)))
        self.max_scan_items = int(os.getenv("AUXRIS_MAX_SCAN_ITEMS", "10000"))
        self.max_queued_scans = int(os.getenv("AUXRIS_MAX_QUEUED_SCANS", "10"))
        self.max_queued_deletions = int(os.getenv("AUXRIS_MAX_QUEUED_DELETIONS", "100"))
        self.allowed_hosts = [
            host.strip()
            for host in os.getenv("AUXRIS_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(
                ","
            )
            if host.strip()
        ]

    def _api_keys(self) -> dict[str, str]:
        keys: dict[str, str] = {}
        raw = os.getenv("AUXRIS_API_KEYS_JSON", "")
        if raw:
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("AUXRIS_API_KEYS_JSON must contain valid JSON") from exc
            if not isinstance(value, dict) or not all(
                isinstance(key, str) and key and isinstance(tenant, str) and tenant
                for key, tenant in value.items()
            ):
                raise ValueError("AUXRIS_API_KEYS_JSON must map non-empty API keys to tenant IDs")
            keys.update(value)
        if self.api_key:
            keys[self.api_key] = self.tenant_id
        return keys


@dataclass(frozen=True, slots=True)
class Principal:
    tenant_id: str


class DatasetScanBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_uri: str = Field(min_length=1, max_length=4_096)
    manifest_sha256: str = Field(pattern="^[a-f0-9]{64}$")
    catalog_ids: list[str] = Field(min_length=1, max_length=20)
    policy_bundle: dict[str, Any]
    detector_profile: str = Field(default="constellation-v1", min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    prior_scan_id: str | None = Field(default=None, max_length=200)


class ReviewDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: str = Field(pattern="^(allow|review|quarantine|block)$")
    reason: str = Field(min_length=1, max_length=4_096)
    reviewer: str | None = Field(default=None, max_length=200)


class DeletionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["scan", "catalog_asset"]
    target_id: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=1, max_length=200)


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.catalog_registry = TenantCatalogRegistry(
            settings.database,
            settings.tenant_catalog_root,
            default_tenant_id=settings.tenant_id,
        )
        app.state.catalog = app.state.catalog_registry.get_catalog(settings.tenant_id)
        app.state.settings = settings
        app.state.dataset_guard = DatasetGuard(
            settings.database,
            app.state.catalog,
            catalog_provider=app.state.catalog_registry,
            manifest_root=settings.manifest_root,
            media_root=settings.media_root,
            max_manifest_bytes=settings.max_manifest_bytes,
            max_media_bytes=settings.max_media_bytes,
            max_items_per_scan=settings.max_scan_items,
            max_queued_per_tenant=settings.max_queued_scans,
        )
        app.state.dataset_guard.recover_incomplete_scans()
        app.state.deletions = DeletionManager(
            settings.database,
            app.state.catalog_registry,
            max_queued_per_tenant=settings.max_queued_deletions,
        )
        app.state.deletions.recover_incomplete_jobs()
        app.state.dataset_worker_error = None
        stop_worker = asyncio.Event()
        worker = asyncio.create_task(
            _dataset_worker(
                app,
                app.state.dataset_guard,
                app.state.deletions,
                stop_worker,
            )
        )
        try:
            yield
        finally:
            stop_worker.set()
            await worker

    application = FastAPI(
        title="Auxris API",
        summary="Audio provenance search for catalogs you control",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @application.middleware("http")
    async def reject_oversized_requests(request: Request, call_next: Any) -> Any:
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > settings.max_upload_bytes + 64 * 1024:
                    return _too_large_response()
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
        return await call_next(request)

    @application.get("/healthz", tags=["operations"])
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @application.get("/readyz", tags=["operations"])
    def ready(request: Request) -> dict[str, Any]:
        catalog: Catalog = request.app.state.catalog
        if request.app.state.dataset_worker_error is not None:
            raise HTTPException(status_code=503, detail="Dataset Guard worker is unhealthy")
        return {"status": "ready", "catalog": catalog.stats()}

    @application.get("/v1/catalog")
    def catalog(
        request: Request,
        principal: Annotated[Principal, Depends(require_api_key)],
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        engine: Catalog = request.app.state.catalog_registry.get_catalog(principal.tenant_id)
        return {
            "stats": engine.stats(),
            "assets": [asset.to_dict() for asset in engine.list_assets(limit=limit, offset=offset)],
        }

    @application.post(
        "/v1/assets",
        status_code=status.HTTP_201_CREATED,
    )
    async def create_asset(
        request: Request,
        principal: Annotated[Principal, Depends(require_api_key)],
        audio: Annotated[UploadFile, File()],
        asset_id: Annotated[str, Form(min_length=1, max_length=200)],
        title: Annotated[str | None, Form(max_length=500)] = None,
        creator: Annotated[str | None, Form(max_length=500)] = None,
        license_id: Annotated[str | None, Form(max_length=200)] = None,
        source_uri: Annotated[str | None, Form(max_length=2_000)] = None,
        c2pa_manifest_uri: Annotated[str | None, Form(max_length=2_000)] = None,
        allowed_uses: Annotated[str, Form(description="Comma-separated policy identifiers")] = "",
    ) -> dict[str, Any]:
        path = await save_bounded_upload(audio, request.app.state.settings.max_upload_bytes)
        try:
            asset = Asset(
                asset_id=asset_id,
                title=title,
                creator=creator,
                license_id=license_id,
                source_uri=source_uri,
                c2pa_manifest_uri=c2pa_manifest_uri,
                allowed_uses=tuple(
                    item.strip() for item in allowed_uses.split(",") if item.strip()
                ),
            )
            engine: Catalog = request.app.state.catalog_registry.get_catalog(principal.tenant_id)
            return engine.index_path(path, asset)
        except CatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            path.unlink(missing_ok=True)

    @application.post("/v1/identify")
    async def identify(
        request: Request,
        principal: Annotated[Principal, Depends(require_api_key)],
        audio: Annotated[UploadFile, File()],
        intended_use: Annotated[str | None, Form(max_length=200)] = None,
    ) -> dict[str, Any]:
        path = await save_bounded_upload(audio, request.app.state.settings.max_upload_bytes)
        try:
            engine: Catalog = request.app.state.catalog_registry.get_catalog(principal.tenant_id)
            matches = engine.identify_path(path, intended_use=intended_use)
            return {
                "matched": bool(matches),
                "algorithm": engine.fingerprinter.algorithm,
                "matches": [match.to_dict() for match in matches],
            }
        finally:
            path.unlink(missing_ok=True)

    @application.post("/v2/dataset-scans", status_code=status.HTTP_202_ACCEPTED)
    def create_dataset_scan(
        request: Request,
        body: DatasetScanBody,
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, Any]:
        guard: DatasetGuard = request.app.state.dataset_guard
        try:
            scan, created = guard.create_scan(
                principal.tenant_id,
                ScanRequest(
                    manifest_uri=body.manifest_uri,
                    manifest_sha256=body.manifest_sha256,
                    catalog_ids=tuple(body.catalog_ids),
                    policy_bundle=body.policy_bundle,
                    detector_profile=body.detector_profile,
                    idempotency_key=body.idempotency_key,
                    prior_scan_id=body.prior_scan_id,
                ),
            )
            return {"created": created, **scan}
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DatasetGuardConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DatasetGuardQuotaExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except DatasetGuardError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/v2/dataset-scans/{scan_id}")
    def get_dataset_scan(
        request: Request,
        scan_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, Any]:
        try:
            guard: DatasetGuard = request.app.state.dataset_guard
            return guard.get_scan(principal.tenant_id, scan_id)
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/v2/dataset-scans/{scan_id}/items")
    def get_dataset_scan_items(
        request: Request,
        scan_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        try:
            items = request.app.state.dataset_guard.list_items(
                principal.tenant_id, scan_id, limit=limit, offset=offset
            )
            return {"scan_id": scan_id, "limit": limit, "offset": offset, "items": items}
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/v2/dataset-scans/{scan_id}/cancel")
    def cancel_dataset_scan(
        request: Request,
        scan_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, Any]:
        try:
            guard: DatasetGuard = request.app.state.dataset_guard
            return guard.request_cancellation(principal.tenant_id, scan_id)
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.get("/v2/dataset-scans/{scan_id}/export.jsonl")
    def export_dataset_scan(
        request: Request,
        scan_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> Response:
        try:
            payload = request.app.state.dataset_guard.export_jsonl(principal.tenant_id, scan_id)
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DatasetGuardConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(
            payload,
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{scan_id}.jsonl"'},
        )

    @application.post("/v2/reviews/{evidence_id}/decisions", status_code=201)
    def create_review_decision(
        request: Request,
        evidence_id: Annotated[str, ApiPath(max_length=200)],
        body: ReviewDecisionBody,
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, Any]:
        try:
            guard: DatasetGuard = request.app.state.dataset_guard
            return guard.add_review_decision(
                principal.tenant_id,
                evidence_id,
                outcome=cast(Decision, body.outcome),
                reason=body.reason,
                reviewer=body.reviewer,
            )
        except DatasetGuardNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DatasetGuardError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.post("/v2/deletions", status_code=status.HTTP_202_ACCEPTED)
    def create_deletion(
        request: Request,
        body: DeletionBody,
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, object]:
        manager: DeletionManager = request.app.state.deletions
        try:
            deletion, created = manager.create_deletion(
                principal.tenant_id,
                DeletionRequest(
                    target_type=body.target_type,
                    target_id=body.target_id,
                    idempotency_key=body.idempotency_key,
                ),
            )
            return {"created": created, **deletion}
        except DeletionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeletionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeletionQuotaExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except DeletionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/v2/deletions/{deletion_id}")
    def get_deletion(
        request: Request,
        deletion_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, object]:
        manager: DeletionManager = request.app.state.deletions
        try:
            return manager.get_deletion(principal.tenant_id, deletion_id)
        except DeletionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @application.post("/v2/deletions/{deletion_id}/retry")
    def retry_deletion(
        request: Request,
        deletion_id: Annotated[str, ApiPath(max_length=200)],
        principal: Annotated[Principal, Depends(require_api_key)],
    ) -> dict[str, object]:
        manager: DeletionManager = request.app.state.deletions
        try:
            return manager.retry_deletion(principal.tenant_id, deletion_id)
        except DeletionNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except DeletionConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except DeletionQuotaExceeded as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc

    @application.get("/", include_in_schema=False)
    def web_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    api_keys: dict[str, str] = request.app.state.settings.api_keys
    if not api_keys:
        raise HTTPException(status_code=503, detail="AUXRIS_API_KEY is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    tenant_id = None
    for expected, tenant in api_keys.items():
        if hmac.compare_digest(supplied, expected):
            tenant_id = tenant
    if scheme.lower() != "bearer" or tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(tenant_id=tenant_id)


async def _dataset_worker(
    app: FastAPI,
    guard: DatasetGuard,
    deletions: DeletionManager,
    stop: asyncio.Event,
) -> None:
    while not stop.is_set():
        try:
            worked = await asyncio.to_thread(deletions.run_next_deletion)
            if not worked:
                worked = await asyncio.to_thread(guard.run_next_scan)
            app.state.dataset_worker_error = None
        except Exception as exc:
            app.state.dataset_worker_error = str(exc)[:1_024]
            LOGGER.exception("Dataset Guard worker iteration failed")
            worked = False
        if worked:
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=0.1)


async def save_bounded_upload(upload: UploadFile, maximum_bytes: int) -> Path:
    suffix = Path(upload.filename or "upload.bin").suffix[:16]
    total = 0
    with tempfile.NamedTemporaryFile(prefix="auxris-", suffix=suffix, delete=False) as handle:
        path = Path(handle.name)
        try:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise HTTPException(status_code=413, detail="Upload exceeds configured limit")
                handle.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="Audio upload is empty")
    return path


def _too_large_response() -> Any:
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=413, content={"detail": "Request exceeds configured limit"})
