# Benchmarking and current evidence

Auxris publishes benchmark inputs, thresholds, individual cases, and limitations with every aggregate. A score without those conditions is not an Auxris capability claim.

## Synthetic transformation baseline

The Phase 2A harness generates four eight-second references and derives 32 positive/negative queries. The material is generated locally and can be redistributed without third-party media rights.

Run it with:

```bash
uv run auxris-benchmark --output benchmark-report.json
```

Observed on 2026-08-02 using the Python reference engine on macOS arm64, default thresholds (`minimum_votes=8`, `minimum_coverage=0.015`), and corpus SHA-256 `f0715791734550c25a7151ce034d991d32fe760b3df53ccb7a454540f701ca31`:

| Transformation | Correct | Queries |
| --- | ---: | ---: |
| Frame-aligned excerpt | 4 | 4 |
| Unaligned 137-sample crop | 4 | 4 |
| -9 dB gain | 4 | 4 |
| 8 kHz resampling round trip | 4 | 4 |
| 3 kHz low-pass | 4 | 4 |
| 20 dB SNR noise | 3 | 4 |
| 3% speed/pitch increase | 0 | 4 |
| Unrelated negative rejected | 4 | 4 |

Overall positive recall was 23/28 (0.821429), precision was 1.0, and all four unrelated negatives were rejected. Those results include one noisy false negative and four speed-shift false negatives. The failures are deliberately published: v1 is not robust to time/pitch changes, and this small synthetic result cannot establish real-world noise performance.

This is a deterministic regression corpus, not a population estimate. Real-world claims require licensed recordings, independent catalog partitions, confidence intervals, codec/device diversity, and a partner-governed evaluation protocol.

The corpus digest covers the fixed-point source bytes and ordered case manifest. Each case separately records the exact transformed query digest, so platform-specific numerical differences remain visible without changing the identity of the benchmark corpus.
