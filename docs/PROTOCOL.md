# Evidence protocol

The HTTP API is currently the canonical interoperability boundary. OpenAPI is generated from the running FastAPI application at `/openapi.json`.

## Stability

- `/v1` response fields will change only through documented migration notes after the first stable release.
- The current package is pre-alpha; pin exact versions when evaluating it.
- `auxris.constellation.v1` fingerprints are implementation details until golden cross-language vectors are published.

## Match semantics

`score` is a bounded ranking score derived from aligned fingerprint coverage. It is **not a probability of infringement, ownership, or factual provenance**.

`aligned_votes` counts landmark collisions agreeing on the strongest time offset. `coverage` divides that count by the query fingerprint count. Source and query time ranges summarize the landmarks supporting that alignment.

`matched: false` means no candidate passed the configured evidence thresholds. It does not mean the query is original, public domain, AI-generated, or copyright-free.

## Compatibility work before v0.2

The normative landmark serialization, hash vectors, and deterministic 1,097-fingerprint signal vector live in `tests/fixtures/constellation-v1.json` and are exercised by Python and Rust. Match-evidence vectors remain required before v0.2 compatibility is declared.

The proposed long-term contracts are recorded in [RFC-0001](rfcs/0001-evidence-taxonomy.md) and [RFC-0002](rfcs/0002-fingerprint-protocol.md). Until those RFCs are accepted, the Rust crate and schemas are experimental.
