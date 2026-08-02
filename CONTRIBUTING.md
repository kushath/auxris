# Contributing to Auxris

Thank you for helping build trustworthy audio provenance infrastructure. Auxris values measured behavior, small reviewable changes, and honest capability boundaries.

## Before contributing

1. Search existing issues and the roadmap.
2. Open a feature proposal before implementing a new public API, algorithm, storage backend, or policy interpretation.
3. Never upload media unless you own it or can redistribute it under a documented license.
4. Use the private process in `SECURITY.md` for vulnerabilities.

## Development setup

```bash
uv sync --all-extras --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src/auxris

# When changing the native kernel:
cargo fmt --all --check
cargo clippy --workspace --all-targets --all-features -- -D warnings
cargo test --workspace --all-features
```

Use Python 3.11 or newer and Rust 1.85 or newer. Commit `uv.lock` and `Cargo.lock` changes when dependencies change. Pull requests should include tests for behavior and update documentation for user-visible changes.

## Algorithm contributions

A new fingerprint or similarity algorithm must include:

- A unique, versioned algorithm identifier.
- Deterministic golden fixtures.
- Known-positive and known-negative evaluation cases.
- Transformation coverage and failure analysis.
- Runtime, memory, and index-size measurements.
- Training-data and model-license documentation if learning is involved.
- A migration or rebuild strategy.

Do not describe a ranking score as a probability unless it has been independently calibrated and documented.

## Developer Certificate of Origin

Auxris uses the [Developer Certificate of Origin 1.1](https://developercertificate.org/). Sign each commit with `git commit -s` to certify that you have the right to submit the contribution under the project license.

## Review expectations

Maintainers may decline features that expand the attack surface or platform footprint before the core evidence is measured. One approving maintainer and green required checks are needed before merge. Security-sensitive changes require a second review when a second maintainer is available.
