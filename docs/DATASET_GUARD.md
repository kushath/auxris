# Dataset Guard experimental reference

Dataset Guard turns the local Auxris catalog into a durable dataset-scanning workflow. An
operator submits a digest-pinned JSON Lines manifest, Auxris evaluates each referenced audio
item against the operator-controlled catalog, and the service emits immutable evidence plus a
separate human-review history.

This is an experimental Phase 2B vertical slice. It is useful for local evaluation and design
partner integration; it is not yet the Phase 2B exit release.

## Security boundary

- Only `file://` manifest and media URIs are accepted.
- Every resolved path must remain beneath `AUXRIS_MANIFEST_ROOT` or `AUXRIS_MEDIA_ROOT`, including
  after symlink resolution.
- The manifest SHA-256 is mandatory. Per-item media SHA-256 values are optional but recommended.
- Manifest, media, item-count, policy-bundle, and active-queue limits are enforced before work.
- API keys map to tenant IDs; scans, items, exports, reviews, and idempotency keys are tenant scoped.
- The current `default` catalog is operator-owned and shared by the reference process. Separate
  tenant catalogs are required before claiming the RFC's zero cross-tenant candidate-leakage exit
  gate.
- The worker does not fetch HTTP URLs, execute callbacks, or load plugins from manifest content.

Treat the configured roots as trusted operator staging areas. Do not expose the reference server
directly to the public internet.

## Manifest

Each non-empty line is one `auxris.dataset.v1` manifest item:

```json
{"item_id":"training:0001","media_uri":"file:///srv/auxris/media/0001.wav","media_sha256":"<64 lowercase hex>","intended_use":"training"}
```

`item_id` must be unique within the manifest. Unsupported fields are rejected so misspellings do
not silently change policy behavior. The public item and export contracts live under
`schemas/auxris/dataset/v1`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUXRIS_API_KEY` | unset | Single-tenant bearer key |
| `AUXRIS_TENANT_ID` | `default` | Tenant attached to the single bearer key |
| `AUXRIS_API_KEYS_JSON` | unset | JSON object mapping bearer keys to tenant IDs |
| `AUXRIS_MANIFEST_ROOT` | `<database-dir>/manifests` | Permitted manifest root |
| `AUXRIS_MEDIA_ROOT` | `<database-dir>/media` | Permitted media root |
| `AUXRIS_MAX_MANIFEST_BYTES` | 10 MiB | Bounded manifest read |
| `AUXRIS_MAX_MEDIA_BYTES` | 2 GiB | Per-item hashing/decoding ceiling |
| `AUXRIS_MAX_SCAN_ITEMS` | 10,000 | Items accepted per scan |
| `AUXRIS_MAX_QUEUED_SCANS` | 10 | Active scans accepted per tenant |

## API workflow

1. `POST /v2/dataset-scans` with `manifest_uri`, `manifest_sha256`,
   `catalog_ids: ["default"]`, `policy_bundle`, `detector_profile`, and an idempotency key.
2. Poll `GET /v2/dataset-scans/{scan_id}` and paginate
   `GET /v2/dataset-scans/{scan_id}/items`.
3. Use `POST /v2/dataset-scans/{scan_id}/cancel` for best-effort idempotent cancellation.
4. Append a human outcome with `POST /v2/reviews/{evidence_id}/decisions`. This never rewrites
   detector evidence.
5. Download the terminal result from `GET /v2/dataset-scans/{scan_id}/export.jsonl`.

The SQLite worker claims queued scans atomically. On startup, interrupted `running` scans return to
the queue and completed items are skipped, preventing duplicate evidence.

## Deliberate gaps

Postgres and object-store interfaces, separate tenant catalogs, deletion jobs, callbacks,
Croissant export, multiple workers under load, peak-memory reporting, and the 30-second queue SLO
remain Phase 2B work. The transformation benchmark is still synthetic, and a no-match result never
means that an item is unprotected or legally safe to train on.
