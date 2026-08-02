# Transformation benchmark

This harness measures regressions against deterministic, generated audio. It requires no third-party media and does not make a real-world accuracy claim.

```bash
uv run auxris-benchmark --output benchmark-report.json
```

The report records the exact source/manifest corpus digest, each transformed query digest, thresholds, per-transformation results, individual cases, latency, throughput, and limitations. Default transformations cover frame-aligned and unaligned excerpts, gain, additive noise, an 8 kHz resampling round trip, low-pass filtering, and a 3% speed/pitch change. Unrelated synthetic queries measure false matches.

Use the default result only as a software regression gate. A publishable accuracy claim additionally needs a redistributable or independently governed corpus, license documentation, catalog-size analysis, confidence intervals, and evaluation by recording type and duration.
