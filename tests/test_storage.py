from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from auxris.dataset_guard.storage import LocalFileObjectStore, ObjectStoreError


def test_local_object_store_is_bounded_and_root_confined(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    inside = root / "inside.bin"
    inside.write_bytes(b"bounded-object")
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"outside")
    store = LocalFileObjectStore(root, object_kind="test")

    assert store.read_bytes(inside.as_uri(), 64) == b"bounded-object"
    store.validate_uri(inside.as_uri())
    with store.materialize(inside.as_uri(), 64) as materialized:
        assert materialized.path == inside.resolve()
        assert materialized.size_bytes == len(b"bounded-object")
        assert materialized.sha256 == hashlib.sha256(b"bounded-object").hexdigest()

    with pytest.raises(ObjectStoreError, match="byte limit"):
        store.read_bytes(inside.as_uri(), 1)
    with (
        pytest.raises(ObjectStoreError, match="byte limit"),
        store.materialize(inside.as_uri(), 1),
    ):
        pass
    with pytest.raises(ObjectStoreError, match="outside"):
        store.validate_uri(outside.as_uri())
    with pytest.raises(ObjectStoreError, match="file://"):
        store.validate_uri("https://example.test/object")
    with pytest.raises(ObjectStoreError, match="query or fragment"):
        store.validate_uri(f"{inside.as_uri()}?version=1")
