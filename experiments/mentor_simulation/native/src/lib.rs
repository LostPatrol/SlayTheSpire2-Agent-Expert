//! Register original Mentor Python objects and search their complete in-memory state.
//! Both bindings and production search share one linked core and content registry.
use pyo3::prelude::*;
use upstream_sts2_py::PyCombatState;

/// Execute production operations without serializing or reconstructing a supplied engine state.
#[pyfunction]
#[pyo3(signature = (request_json, state = None))]
fn simulation_handle(
    py: Python<'_>,
    request_json: &str,
    state: Option<PyRef<'_, PyCombatState>>,
) -> PyResult<String> {
    let request: serde_json::Value = serde_json::from_str(request_json)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))?;
    // Clone while the Python borrow is held; search cannot mutate the live truth mirror.
    let engine = state.as_ref().map(|state| state.inner_ref().clone());
    let response = py.allow_threads(move || match engine {
        Some(engine) => mentor_simulation::handle_state(&request, engine),
        None => mentor_simulation::handle(&request),
    });
    serde_json::to_string(&response)
        .map_err(|error| pyo3::exceptions::PyValueError::new_err(error.to_string()))
}

/// Install original classes in this DLL before adding the production search entry point.
#[pymodule]
fn mentor_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    upstream_sts2_py::sts2_rs(module)?;
    module.add_function(wrap_pyfunction!(simulation_handle, module)?)?;
    Ok(())
}
