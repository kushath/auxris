# RFC-0004: Control plane and data plane architecture

- Status: Proposed
- Target: v0.2 through v0.4

## Architecture

Auxris has two deployable planes connected by versioned contracts.

The data plane ingests bounded media, normalizes it, invokes detector adapters, retrieves catalog candidates, and emits evidence. It is stateless except for explicitly configured indexes and queues. The control plane manages tenants, catalogs, provenance statements, policy bundles, detector releases, retention, audit metadata, and human review.

```text
SDK / batch job / generation service
                |
          Scan Gateway
                |
       bounded job or stream
                |
  +-------------+--------------+
  | fingerprint | watermark    |
  | credentials | speaker opt. |
  +-------------+--------------+
                |
       Evidence normalizer
                |
        Policy evaluator
                |
   evidence store + review queue
```

## Deployment profiles

- `embedded`: local library, SQLite catalog, no network, intended for tests and small catalogs.
- `single-node`: API plus worker and persistent catalog for evaluation.
- `distributed`: stateless gateways, durable queue, object storage, Postgres control data, sharded retrieval index, and isolated detector workers.

The open project must remain useful in `embedded` mode with zero paid infrastructure. Distributed components are adapters, not prerequisites.

## Trust boundaries

Media input, metadata, webhooks, policy bundles, model adapters, and remote credential resolvers are untrusted. Gateways enforce byte, duration, codec, concurrency, and deadline limits. Detector workers use least privilege and separate resource pools. Tenant IDs are derived from authenticated context, never trusted from request payloads.

## Reliability

Each scan receives an idempotency key. Queue delivery is at least once; evidence writes are idempotent by `(tenant, scan_id, detector_id, detector_version)`. Dead-letter records retain error metadata, not media. Backpressure is explicit and overload returns retryable status rather than silently sampling requests.

## Observability

Required service-level measurements include accepted/rejected jobs, end-to-end latency, queue age, decoder failures, detector timeouts, landmarks per minute, candidates examined, decision distribution, false-positive review outcomes, and cost per audio hour. Traces carry opaque IDs and must not include raw media or embeddings.

## Extensibility

Detector adapters declare input type, resource requirements, result schema, version, configuration digest, and calibration metadata. Out-of-process adapters are preferred for third-party models. A failed optional detector produces a limitation, not fabricated negative evidence.
