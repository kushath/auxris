#![no_main]

use auxris_core::{fingerprint_samples, FingerprintConfig};
use libfuzzer_sys::fuzz_target;

fuzz_target!(|data: &[u8]| {
    // Cap conversion work independently of the fuzzer's corpus configuration.
    let samples: Vec<f32> = data
        .chunks_exact(4)
        .take(32_768)
        .map(|bytes| f32::from_le_bytes([bytes[0], bytes[1], bytes[2], bytes[3]]))
        .collect();
    let config = FingerprintConfig {
        max_samples: 32_768,
        max_fingerprints: 100_000,
        ..FingerprintConfig::default()
    };
    let _ = fingerprint_samples(&samples, &config);
});
