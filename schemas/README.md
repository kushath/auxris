# Public schemas

These JSON Schemas are versioned interoperability contracts. Their `$id` values are stable identifiers; local consumers should vendor the release they validate against rather than fetching schemas at runtime.

- `auxris.evidence.v1`: portable detector evidence and policy outcome
- `auxris.asset.v1`: attributable asset provenance and usage assertions
- `auxris.policy-decision.v1`: deterministic operational decision
- `auxris.benchmark.v1`: reproducible transformation benchmark report
- `auxris.dataset.v1`: Dataset Guard input items, immutable exports, and deletion jobs

Examples are illustrative and contain no real catalog or identity data. Schema additions remain backward compatible within a major version. Required-field removal, semantic reinterpretation, or type changes require a new major identifier.
