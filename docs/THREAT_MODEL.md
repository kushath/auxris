# Threat model

## Protected assets

- Private reference and query audio.
- Catalog provenance, consent, and license assertions.
- API credentials and auditability of results.
- Service availability and index integrity.
- The reputation of people whose work may be matched incorrectly.

## Trust boundaries

Uploads, catalog metadata, filenames, HTTP headers, media decoders, future outbound integrations, and imported indexes are untrusted. A catalog operator is trusted to assert metadata but may still provide malformed files. Match results are evidence, not authoritative ownership records.

## Current controls

- Bearer authentication on every catalog and identification route.
- No default API credential; the server command generates one for local development.
- Constant-time credential comparison.
- Localhost binding by the CLI and Compose configuration.
- Trusted host allowlist.
- Request and streaming upload byte limits.
- Random temporary filenames and unconditional cleanup.
- No URL ingestion, archive extraction, webhook dispatch, pickle, joblib, or remote model loading.
- Parameterized SQL, transactional replacement, and foreign keys.
- Bearer keys resolve to tenant-separated catalog files; tests exercise candidate and control-record
  isolation through both identification and Dataset Guard.
- Tenant IDs are hashed before catalog paths are constructed.
- Root-confined object-store access, digest-pinned manifests, bounded materialization, and no
  network fetcher.
- Resumable logical deletion with raw target-ID redaction and digest-only tombstones.
- Read-only container root, dropped capabilities, and dedicated data volume.

## Known limitations

- Multipart parsing begins before the endpoint's streaming limit; deployers must also enforce a reverse-proxy body limit.
- Audio decoding relies on libsndfile and should be isolated further for hostile public uploads.
- API keys remain static shared secrets per tenant; there is no rotation or delegated-identity API.
- SQLite is not suitable for untrusted multi-tenant hosting or large write concurrency.
- Logical deletion cannot erase prior exports, backups, filesystem snapshots, or replicas.
- Digest tombstones for predictable asset IDs can be dictionary guessed and are pseudonymous, not
  anonymous.
- Match thresholds are uncalibrated until the public benchmark corpus is established.
- Catalog metadata is asserted, not cryptographically verified.

## Deployment requirements

Do not expose a pre-alpha instance directly to the public internet. Terminate TLS at a maintained proxy, enforce body and request-rate limits there, restrict source networks where possible, use a generated secret, back up the catalog, and run the service as a non-root user.

Report vulnerabilities through the private process in `SECURITY.md`, never through a public issue.
