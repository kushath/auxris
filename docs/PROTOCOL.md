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

The project will publish a fixture manifest containing canonical PCM samples, expected fingerprints, serialization bytes, and match evidence. Python, future Rust/WASM, and SDK implementations must reproduce those fixtures before claiming compatibility.

