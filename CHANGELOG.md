# Changelog

All notable changes will be documented here. The project follows semantic versioning after the first stable release.

## [Unreleased]

### Added

- Proposed RFCs for evidence semantics, fingerprint compatibility, consent policy, runtime architecture, and Dataset Guard acceptance.
- Versioned JSON Schemas for portable evidence, asset provenance, and policy decisions.
- Experimental `auxris-core` Rust kernel with hard sample/output limits and bounded chunk ingestion.
- Rolling extraction that retains only STFT overlap, local spectra, and candidate peaks.
- Optional `auxris-native` ABI3 wheel with batch and stateful NumPy interfaces.
- Versioned synthetic transformation benchmark report and public failure baseline.
- Experimental Dataset Guard `/v2` job API with durable resumable scans, digest-pinned manifests,
  tenant-scoped records, immutable JSONL evidence, quotas, cancellation, and human review history.
- Versioned Dataset Guard manifest-item and export-record JSON Schemas.
- Tenant-separated candidate catalogs with hashed paths and cross-tenant leakage tests.
- Pluggable Dataset Guard object-store protocol with a bounded, root-confined local adapter.
- Resumable scan/catalog deletion jobs with digest-only tombstones and a public job schema.
- Exact Python/Rust landmark and signal-level golden vectors.
- Rust formatting, Clippy, tests, Dependabot coverage, and scheduled fuzzing.
- Deterministic constellation fingerprint engine.
- Durable SQLite catalog with temporal-offset matching evidence.
- CLI for indexing, scanning, catalog inspection, and local serving.
- Authenticated FastAPI adapter with bounded uploads.
- Local provenance console.
- Explicit usage-policy result.
- Synthetic end-to-end demo and restart/rejection tests.
- Container, CI, dependency update, security scanning, governance, and contributor foundations.
