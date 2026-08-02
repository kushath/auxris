"""Authenticated HTTP adapter around the deterministic Auxris core."""

from __future__ import annotations

import hmac
import os
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile, status
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from auxris import __version__
from auxris.core.index import Catalog, CatalogError
from auxris.core.models import Asset

STATIC_DIR = Path(__file__).parent / "static"


class Settings:
    def __init__(self) -> None:
        self.database = Path(os.getenv("AUXRIS_DB_PATH", "./data/auxris.sqlite3")).resolve()
        self.api_key = os.getenv("AUXRIS_API_KEY", "")
        self.max_upload_bytes = int(os.getenv("AUXRIS_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
        self.allowed_hosts = [
            host.strip()
            for host in os.getenv("AUXRIS_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(
                ","
            )
            if host.strip()
        ]


def create_app() -> FastAPI:
    settings = Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.catalog = Catalog(settings.database)
        app.state.settings = settings
        yield

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
        return {"status": "ready", "catalog": catalog.stats()}

    @application.get("/v1/catalog", dependencies=[Depends(require_api_key)])
    def catalog(request: Request, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        engine: Catalog = request.app.state.catalog
        return {
            "stats": engine.stats(),
            "assets": [asset.to_dict() for asset in engine.list_assets(limit=limit, offset=offset)],
        }

    @application.post(
        "/v1/assets",
        status_code=status.HTTP_201_CREATED,
        dependencies=[Depends(require_api_key)],
    )
    async def create_asset(
        request: Request,
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
            engine: Catalog = request.app.state.catalog
            return engine.index_path(path, asset)
        except CatalogError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            path.unlink(missing_ok=True)

    @application.post("/v1/identify", dependencies=[Depends(require_api_key)])
    async def identify(
        request: Request,
        audio: Annotated[UploadFile, File()],
        intended_use: Annotated[str | None, Form(max_length=200)] = None,
    ) -> dict[str, Any]:
        path = await save_bounded_upload(audio, request.app.state.settings.max_upload_bytes)
        try:
            matches = request.app.state.catalog.identify_path(path, intended_use=intended_use)
            return {
                "matched": bool(matches),
                "algorithm": request.app.state.catalog.fingerprinter.algorithm,
                "matches": [match.to_dict() for match in matches],
            }
        finally:
            path.unlink(missing_ok=True)

    @application.get("/", include_in_schema=False)
    def web_console() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    application.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return application


def require_api_key(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.api_key
    if not expected:
        raise HTTPException(status_code=503, detail="AUXRIS_API_KEY is not configured")
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid bearer token required",
            headers={"WWW-Authenticate": "Bearer"},
        )


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
