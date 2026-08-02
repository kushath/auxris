"""Tenant-separated catalog provider for the single-node reference service."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Protocol

from auxris.core.index import Catalog


class CatalogProvider(Protocol):
    """Resolve the only catalog a tenant is authorized to query."""

    def get_catalog(self, tenant_id: str) -> Catalog:
        """Return a catalog isolated to ``tenant_id``."""


class StaticCatalogProvider:
    """Compatibility provider for embedded single-catalog callers."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def get_catalog(self, tenant_id: str) -> Catalog:
        del tenant_id
        return self.catalog


class TenantCatalogRegistry:
    """Map tenant IDs to separate SQLite files without using IDs as path components."""

    def __init__(
        self,
        legacy_default_database: str | Path,
        catalog_root: str | Path,
        *,
        default_tenant_id: str = "default",
    ) -> None:
        self.legacy_default_database = Path(legacy_default_database).resolve()
        self.catalog_root = Path(catalog_root).resolve()
        self.default_tenant_id = default_tenant_id
        self.catalog_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._catalogs: dict[str, Catalog] = {}

    def get_catalog(self, tenant_id: str) -> Catalog:
        if not tenant_id or len(tenant_id) > 200 or any(ord(char) < 32 for char in tenant_id):
            raise ValueError("tenant_id must contain 1 to 200 printable characters")
        digest = hashlib.sha256(tenant_id.encode()).hexdigest()
        with self._lock:
            catalog = self._catalogs.get(digest)
            if catalog is not None:
                return catalog
            path = self._catalog_path(tenant_id, digest)
            existed = path.exists()
            catalog = Catalog(path)
            if not existed:
                path.chmod(0o600)
            self._catalogs[digest] = catalog
            return catalog

    def _catalog_path(self, tenant_id: str, digest: str) -> Path:
        if tenant_id == self.default_tenant_id:
            return self.legacy_default_database
        return self.catalog_root / f"tenant-{digest}.sqlite3"
