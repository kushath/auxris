# RFC-0005: Dataset Guard API and benchmark

- Status: Proposed
- Target: v0.2

## Product slice

Dataset Guard scans an audio dataset against catalogs and policies controlled by the operator, creates an immutable manifest, and routes ambiguous or disallowed items for review. It is the first production-oriented Auxris workflow because it is useful before any proprietary model training is available.

## Job API

`POST /v2/dataset-scans` accepts a manifest reference, catalog IDs, policy bundle, detector profile, idempotency key, and optional callback. Media is referenced by operator-controlled URIs or uploaded through a separately bounded endpoint; bulk media is not embedded in JSON.

`GET /v2/dataset-scans/{id}` returns state and aggregate counts. `GET /v2/dataset-scans/{id}/items` paginates item evidence. `POST /v2/reviews/{id}/decisions` records a human decision without rewriting detector evidence.

States are `queued`, `running`, `completed`, `completed_with_errors`, `cancelled`, and `failed`. Cancellation is best effort and idempotent.

## Immutable manifest

Each run records input manifest digest, item URI and media digest, detector and policy versions, evidence IDs, decision, timestamps, and errors. Export supports JSON Lines and a Croissant mapping. Re-running creates a new scan linked to the earlier scan.

## Benchmark corpus

The public benchmark must use assets with explicit redistribution and transformation permission. It contains clean references plus deterministic transformations: time shift, excerpting, gain, lossy encoding, resampling, additive noise, equalization, pitch shift, and time stretch. Unrelated Creative Commons or public-domain recordings form negative pairs.

Private partner assets are evaluated by a reproducible harness that exports aggregate metrics without publishing media or fingerprints.

## Acceptance gates

The v0.2 release requires:

- 100% golden-vector compatibility across Python and Rust hashing;
- deterministic results across chunk boundaries for the streaming profile;
- benchmark reports stratified by transformation and clip duration;
- reported precision, recall, false matches per query-hour, p50/p95 latency, peak memory, and throughput;
- zero cross-tenant candidate leakage tests;
- resumable scans and idempotent evidence writes;
- documented dataset licenses and an empty-by-default demonstration catalog.

No single accuracy number may be advertised without its corpus, catalog size, thresholds, and confidence interval.

## Initial service objectives

For the single-node reference profile on published hardware: 95% of five-minute items begin processing within 30 seconds under the documented load, jobs survive worker restart without duplicate evidence, and the service reports overload explicitly. Accuracy targets will be set only after the licensed benchmark exists.
