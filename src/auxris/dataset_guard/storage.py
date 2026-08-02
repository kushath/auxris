"""Object-store boundary and zero-infrastructure local implementation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse


class ObjectStoreError(RuntimeError):
    """Raised when an object URI cannot be accessed within configured bounds."""


@dataclass(frozen=True, slots=True)
class MaterializedObject:
    """A locally readable object and the digest of its bounded bytes."""

    path: Path
    sha256: str
    size_bytes: int


class ObjectStore(Protocol):
    """Minimal storage interface required by Dataset Guard."""

    def read_bytes(self, uri: str, maximum_bytes: int) -> bytes:
        """Read one bounded object into memory."""

    def validate_uri(self, uri: str) -> None:
        """Reject an object URI that this store cannot safely serve."""

    def materialize(
        self, uri: str, maximum_bytes: int
    ) -> AbstractContextManager[MaterializedObject]:
        """Yield a bounded object as a local file for decoders that require a path."""


class LocalFileObjectStore:
    """Root-confined ``file://`` object store for local and isolated deployments."""

    def __init__(self, root: str | Path, *, object_kind: str) -> None:
        self.root = Path(root).resolve()
        self.object_kind = object_kind

    def read_bytes(self, uri: str, maximum_bytes: int) -> bytes:
        path = self._resolve(uri)
        try:
            with path.open("rb") as handle:
                payload = handle.read(maximum_bytes + 1)
        except OSError as exc:
            raise ObjectStoreError(f"The {self.object_kind} URI is not readable") from exc
        if len(payload) > maximum_bytes:
            raise ObjectStoreError(
                f"The {self.object_kind} object exceeds the configured byte limit"
            )
        return payload

    def validate_uri(self, uri: str) -> None:
        self._resolve(uri)

    @contextmanager
    def materialize(self, uri: str, maximum_bytes: int) -> Iterator[MaterializedObject]:
        path = self._resolve(uri)
        digest = hashlib.sha256()
        total = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    total += len(chunk)
                    if total > maximum_bytes:
                        raise ObjectStoreError(
                            f"The {self.object_kind} object exceeds the configured byte limit"
                        )
                    digest.update(chunk)
        except OSError as exc:
            raise ObjectStoreError(f"The {self.object_kind} URI is not readable") from exc
        yield MaterializedObject(path=path, sha256=digest.hexdigest(), size_bytes=total)

    def _resolve(self, uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
            raise ObjectStoreError(
                f"Only local file:// {self.object_kind} URIs are supported by this store"
            )
        if parsed.query or parsed.fragment:
            raise ObjectStoreError(f"The {self.object_kind} URI cannot contain a query or fragment")
        try:
            candidate = Path(unquote(parsed.path)).resolve(strict=True)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ObjectStoreError(
                f"The {self.object_kind} URI does not resolve to a readable file"
            ) from exc
        if not candidate.is_file() or not candidate.is_relative_to(self.root):
            raise ObjectStoreError(f"The {self.object_kind} URI is outside its configured root")
        return candidate
