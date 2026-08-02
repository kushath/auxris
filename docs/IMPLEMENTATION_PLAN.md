# Six-month implementation plan

This plan turns the v0.1 reference application into a testable audio trust platform while keeping a zero-cost embedded path. Dates are relative to project start and depend on maintainer capacity.

Current Phase 2A progress: the public schemas, rolling Rust kernel, optional Python binding, cross-language signal vector, fuzz target, and generated-audio benchmark harness are implemented in the v0.2 draft. Match-evidence vectors and a partner-approved transformation corpus remain before the phase exit.

Current Phase 2B progress: an experimental SQLite job state machine now provides digest-pinned
local manifests, resumable item processing, tenant-scoped control records, idempotency, quotas,
immutable JSONL evidence, cancellation, audit events, and append-only review decisions. Postgres
remains pending; tenant-separated catalogs, the object-store interface and local adapter, and
resumable deletion jobs are implemented. Remote object stores, Croissant export, load/SLO evidence,
and partner evaluation remain before the phase exit.

## Phase 2A — protocol foundation (weeks 1–8)

- accept RFC-0001 through RFC-0005;
- publish JSON Schemas, examples, and compatibility policy;
- ship the Rust constellation kernel with Python parity vectors;
- add chunk-boundary, property, fuzz, malformed-input, and resource-limit tests;
- publish the first transformation benchmark harness and dataset contribution rules.

Exit: v0.2.0-alpha.1 can produce and validate portable evidence without a hosted service.

## Phase 2B — Dataset Guard (weeks 9–16)

- manifest-driven asynchronous scanning and resumable workers;
- Postgres control-store and object-store interfaces alongside SQLite;
- immutable JSONL/Croissant result manifests;
- quarantine and human-review workflow;
- tenant isolation, idempotency, quotas, audit events, and deletion jobs;
- public benchmark report with reproducible hardware and thresholds.

Exit: two design partners complete datasets in isolated evaluation environments.

## Phase 2C — Voice Guard alpha (weeks 17–22)

- pluggable speaker-similarity interface with no bundled identity database;
- enrollment consent, purpose, expiry, revocation, and deletion records;
- calibrated cohort evaluation and spoof/replay test harness;
- optional watermark and C2PA adapter SDKs;
- real-time generation-gateway example using policy outcomes.

Exit: an opt-in voice-model lab can block or review protected-speaker similarity with documented error rates and human escalation.

## Phase 2D — production hardening (weeks 23–26)

- threat-model review, external security audit scope, and incident runbooks;
- signed releases, SBOMs, provenance attestations, and reproducible build work;
- load/soak/failure testing and published capacity model;
- stable SDK ergonomics, deployment charts, migrations, and operator guide;
- governance expansion and a public compatibility program.

Exit: v0.4 enterprise pilot, with v1 criteria agreed from measured deployment evidence.

## Workstreams and ownership

| Workstream | Early deliverables | Maintainer profile |
| --- | --- | --- |
| Protocol | schemas, RFCs, vectors | applied audio / standards |
| Kernel | Rust, Python/WASM bindings | DSP / systems |
| Platform | jobs, storage, tenancy | backend / SRE |
| Policy | consent model, evaluator | security / policy engineering |
| Evaluation | corpus, transforms, metrics | ML evaluation |
| Community | docs, releases, triage | developer relations |

## Cost envelope

Development can remain local and CI-backed through Phase 2A. A small Dataset Guard pilot can run on one CPU server plus object storage; GPU cost appears only when optional neural speaker or watermark adapters are enabled. The project will publish cost per processed audio hour before recommending a hosted topology.

The largest non-compute dependencies are legitimately licensed evaluation assets, maintainer time, security review, and partner integration. Auxris will not train a universal copyrighted-music model in these phases.

## Immediate backlog

1. Review and accept the five RFCs.
2. Stabilize schemas and publish generated documentation.
3. Complete Rust signal-level parity and a Python native binding.
4. Add deterministic transformation fixtures with clear licenses.
5. Implement the Dataset Guard job state machine behind an experimental API.
6. Recruit two catalog holders and one model lab under a private evaluation agreement.
