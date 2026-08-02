//! Cross-language protocol conformance tests.

use std::fmt::Write as _;

use auxris_core::{fingerprint_samples, hash_landmark, FingerprintConfig};
use blake2b_simd::Params;
use serde::Deserialize;

#[derive(Deserialize)]
struct Fixture {
    protocol: String,
    signal_case: SignalCase,
    normative_cases: Vec<Case>,
}

#[derive(Deserialize)]
struct SignalCase {
    seed: u32,
    sample_count: usize,
    fingerprint_count: usize,
    fingerprint_stream_blake2b_256: String,
}

#[derive(Deserialize)]
struct Case {
    anchor_bin: u16,
    target_bin: u16,
    delta_frames: u16,
    payload_hex: String,
    hash_hex: String,
}

fn hex(bytes: &[u8]) -> String {
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        write!(&mut encoded, "{byte:02x}").expect("writing to String cannot fail");
    }
    encoded
}

#[test]
fn shared_hash_vectors_are_exact() {
    let fixture: Fixture = serde_json::from_str(include_str!(
        "../../../tests/fixtures/constellation-v1.json"
    ))
    .expect("valid fixture");
    assert_eq!(fixture.protocol, auxris_core::ALGORITHM);

    for case in fixture.normative_cases {
        let mut payload = Vec::with_capacity(6);
        payload.extend_from_slice(&case.anchor_bin.to_be_bytes());
        payload.extend_from_slice(&case.target_bin.to_be_bytes());
        payload.extend_from_slice(&case.delta_frames.to_be_bytes());
        assert_eq!(hex(&payload), case.payload_hex);
        assert_eq!(
            hex(&hash_landmark(
                case.anchor_bin,
                case.target_bin,
                case.delta_frames
            )),
            case.hash_hex
        );
    }
}

#[test]
fn shared_signal_vector_is_exact() {
    let fixture: Fixture = serde_json::from_str(include_str!(
        "../../../tests/fixtures/constellation-v1.json"
    ))
    .expect("valid fixture");
    let mut state = fixture.signal_case.seed;
    let samples: Vec<f32> = (0..fixture.signal_case.sample_count)
        .map(|_| {
            state = state.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let centered = f32::from((state >> 16) as u16) - 32_768.0;
            centered / 32_768.0
        })
        .collect();
    let fingerprints = fingerprint_samples(&samples, &FingerprintConfig::default())
        .expect("signal vector fingerprints");
    assert_eq!(fingerprints.len(), fixture.signal_case.fingerprint_count);

    let mut stream = Vec::with_capacity(fingerprints.len() * 16);
    for fingerprint in fingerprints {
        stream.extend_from_slice(&fingerprint.hash_bytes);
        stream.extend_from_slice(&fingerprint.frame.to_be_bytes());
    }
    let digest = Params::new()
        .hash_length(32)
        .personal(b"auxrisvec")
        .hash(&stream);
    assert_eq!(
        hex(digest.as_bytes()),
        fixture.signal_case.fingerprint_stream_blake2b_256
    );
}
