//! Optional `CPython` boundary for the resource-bounded Auxris kernel.

use std::fmt::Write as _;

use auxris_core::{fingerprint_samples, Fingerprint, FingerprintConfig, StreamingFingerprinter};
use numpy::PyReadonlyArray1;
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;

fn config_with_limit(max_samples: Option<usize>) -> FingerprintConfig {
    FingerprintConfig {
        max_samples: max_samples.unwrap_or_else(|| FingerprintConfig::default().max_samples),
        ..FingerprintConfig::default()
    }
}

fn encode(fingerprints: Vec<Fingerprint>) -> Vec<(String, u64)> {
    fingerprints
        .into_iter()
        .map(|item| {
            let mut hash_hex = String::with_capacity(16);
            for byte in item.hash_bytes {
                write!(&mut hash_hex, "{byte:02x}").expect("writing to String cannot fail");
            }
            (hash_hex, item.frame)
        })
        .collect()
}

/// Fingerprint one bounded PCM subject and return `(hash_hex, frame)` pairs.
#[pyfunction]
#[pyo3(signature = (samples, max_samples=None))]
#[allow(clippy::needless_pass_by_value)]
fn fingerprint_hex(
    py: Python<'_>,
    samples: PyReadonlyArray1<'_, f32>,
    max_samples: Option<usize>,
) -> PyResult<Vec<(String, u64)>> {
    let samples = samples
        .as_slice()
        .map_err(|_| PyValueError::new_err("Expected a contiguous float32 NumPy array"))?
        .to_vec();
    let config = config_with_limit(max_samples);
    py.detach(move || fingerprint_samples(&samples, &config).map(encode))
        .map_err(|error| PyValueError::new_err(error.to_string()))
}

/// Stateful rolling extractor for arbitrary Python chunk boundaries.
#[pyclass(name = "StreamingFingerprinter")]
struct PyStreamingFingerprinter {
    inner: Option<StreamingFingerprinter>,
}

#[pymethods]
impl PyStreamingFingerprinter {
    #[new]
    #[pyo3(signature = (max_samples=None))]
    fn new(max_samples: Option<usize>) -> PyResult<Self> {
        let inner = StreamingFingerprinter::new(config_with_limit(max_samples))
            .map_err(|error| PyValueError::new_err(error.to_string()))?;
        Ok(Self { inner: Some(inner) })
    }

    #[allow(clippy::needless_pass_by_value)]
    fn push(&mut self, py: Python<'_>, samples: PyReadonlyArray1<'_, f32>) -> PyResult<()> {
        let samples = samples
            .as_slice()
            .map_err(|_| PyValueError::new_err("Expected a contiguous float32 NumPy array"))?
            .to_vec();
        let inner = self.active_mut()?;
        py.detach(move || inner.push(&samples))
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    fn finish(&mut self, py: Python<'_>) -> PyResult<Vec<(String, u64)>> {
        let inner = self
            .inner
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("fingerprinter is already finished"))?;
        py.detach(move || inner.finish().map(encode))
            .map_err(|error| PyValueError::new_err(error.to_string()))
    }

    fn buffered_sample_count(&mut self) -> PyResult<usize> {
        Ok(self.active_mut()?.buffered_sample_count())
    }

    fn retained_candidate_count(&mut self) -> PyResult<usize> {
        Ok(self.active_mut()?.retained_candidate_count())
    }
}

impl PyStreamingFingerprinter {
    fn active_mut(&mut self) -> PyResult<&mut StreamingFingerprinter> {
        self.inner
            .as_mut()
            .ok_or_else(|| PyRuntimeError::new_err("fingerprinter is already finished"))
    }
}

/// Native Auxris protocol module.
#[pymodule]
fn auxris_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("ALGORITHM", auxris_core::ALGORITHM)?;
    module.add_function(wrap_pyfunction!(fingerprint_hex, module)?)?;
    module.add_class::<PyStreamingFingerprinter>()?;
    Ok(())
}
