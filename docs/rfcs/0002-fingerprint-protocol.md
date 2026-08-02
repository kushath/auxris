# RFC-0002: Constellation fingerprint protocol

- Status: Proposed
- Protocol: `auxris.constellation.v1`
- Target: v0.2

## Purpose

The protocol detects exact and transformation-resistant reuse of recordings already present in an operator-controlled catalog. It is a retrieval primitive, not an ownership oracle.

## Canonical input

- mono IEEE-754 float32 PCM;
- 11,025 samples per second;
- finite values normalized to `[-1, 1]`;
- SHA-256 over little-endian canonical PCM for subject identity.

Decoding and resampling are outside the fingerprint protocol. Implementations must disclose their decoder and resampler because those choices can affect boundary cases.

## Landmark extraction

1. Reject clips shorter than 2,048 samples and clips with RMS below `1e-6`.
2. Compute an STFT with a periodic Hann window, FFT size 2,048, hop 512, no boundary extension, and no padding.
3. Convert magnitudes to dB relative to the largest magnitude in the clip, using float32 minimum positive normal as the floor.
4. Select bins at or above -55 dB that equal the maximum in a 17-bin by 5-frame nearest-edge neighborhood.
5. Keep at most five strongest bins per frame; serialize retained peaks in ascending frame and bin order.

## Pairing and hashing

For each anchor peak, select the first eight later peaks whose frame delta is between 1 and 60 inclusive. Serialize `(anchor_bin, target_bin, delta_frames)` as three unsigned 16-bit big-endian integers. Hash the six bytes with BLAKE2b, an 8-byte digest, and personalization `auxrisv1`. Store the digest and anchor frame.

The 64-bit digest is a retrieval key, not a cryptographic content commitment. Candidate matches require aligned vote and coverage thresholds; collisions are expected and handled statistically.

## Determinism

Golden vectors in `tests/fixtures/constellation-v1.json` are normative for serialization and hashing. Signal-level vectors are conformance tests for the reference front end. Implementations claiming protocol compatibility must reproduce every normative vector byte-for-byte.

Floating-point ties may vary across FFT libraries. The Rust kernel therefore uses explicit stable ordering `(magnitude, bin)` and documents any vector whose tolerance is numeric rather than exact. A future v2 may use a quantized fixed-point front end if cross-platform evidence shows it is necessary.

## Streaming profile

A conforming streaming implementation may retain enough frames for the five-frame peak neighborhood and 60-frame pairing horizon. Chunk boundaries must not change output. Because v1's dB floor is relative to the complete subject maximum, the rolling reference extractor retains at most `peaks_per_frame` candidates per processed frame and emits hashes at `finish()`. It does not retain the complete PCM or spectrogram. Buffering an entire bounded clip is allowed only in the `bounded-batch` execution mode and must enforce a caller-configured sample limit.

## Security limits

Implementations must validate configuration arithmetic, cap samples and emitted landmarks, reject non-finite PCM, and avoid allocation based on untrusted metadata. Fingerprints are sensitive catalog-derived data and require access control in multi-tenant deployments.
