# Auxris native Python binding

`auxris-native` is an optional ABI3 wheel for Python 3.11 and newer. It exposes the Rust kernel without making Rust a requirement for the base `auxris` package.

Local development:

```bash
uv sync --all-extras --dev
cd bindings/python
uv run --project ../.. maturin develop
cd ../..
uv run pytest tests/test_native.py --no-cov
```

The extension accepts contiguous one-dimensional NumPy `float32` arrays and returns protocol hash/frame pairs. `auxris.core.native` converts those pairs into the same `Fingerprint` model used by the Python reference engine.

The wheel is experimental and separately versioned. It does not decode media, make policy decisions, contact a service, or silently replace the Python backend.
