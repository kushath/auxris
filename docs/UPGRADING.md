# Upgrading pre-alpha deployments

## Tenant catalog isolation

This release changes multi-key deployments from one shared candidate catalog to one SQLite catalog
per tenant. Stop the service and back up the data directory before upgrading.

- The tenant named by `AUXRIS_TENANT_ID` keeps the legacy catalog at `AUXRIS_DB_PATH`. The default
  remains `default`, preserving existing single-tenant installations.
- Every other tenant receives an empty catalog under `AUXRIS_TENANT_CATALOG_ROOT`. Filenames use
  the full SHA-256 of the tenant ID; raw tenant IDs are never path components.
- Auxris does not automatically copy shared assets into tenant catalogs because the old database
  contains no trustworthy ownership field. Re-index each tenant's assets from its authoritative
  source and provenance metadata.
- Persist and back up both `AUXRIS_DB_PATH` and `AUXRIS_TENANT_CATALOG_ROOT`.

After restart, verify each bearer key independently with `GET /v1/catalog`. A non-default tenant
should report an empty catalog until its own assets are registered.

## Dataset Guard storage

Dataset Guard now depends on an `ObjectStore` interface. The bundled adapter remains a
root-confined local `file://` store, so existing manifest and media paths continue to work.
Future remote adapters must enforce the same URI validation, byte limits, materialization, and
digest semantics.

## Logical deletion

`POST /v2/deletions` creates resumable jobs for terminal scans or tenant catalog assets. Completed
jobs clear the raw target ID and retain its SHA-256 tombstone. This removes active SQLite records,
reviews, evidence, fingerprints, and scan audit details.

A digest tombstone is pseudonymous, not anonymous: predictable asset IDs may be recoverable by
dictionary guessing. Do not put names, email addresses, or other direct personal identifiers in
asset IDs.

Logical deletion is not a promise of cryptographic erasure from filesystem snapshots, replicas,
backups, storage-controller caches, or previously exported JSONL. Operators must apply their own
backup retention and object-store deletion policies and document those guarantees to data owners.
