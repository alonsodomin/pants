// Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
// Licensed under the Apache License, Version 2.0 (see LICENSE).

use std::time::Duration;

use futures::future::TryFutureExt;
use futures::try_join;
use pyo3::types::{PyAnyMethods, PyModule, PyModuleMethods};
use pyo3::{Bound, IntoPyObject, PyResult, Python, pyfunction, wrap_pyfunction};

use crate::externs::{self, PyGeneratorResponseNativeCall};
use crate::nodes::{ExecuteProcess, NodeResult, Snapshot, task_get_context};
use crate::python::Value;

pub fn register(_py: Python, m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(execute_process, m)?)?;

    Ok(())
}

#[pyfunction]
fn execute_process(process: Value, process_config: Value) -> PyGeneratorResponseNativeCall {
    PyGeneratorResponseNativeCall::new(async move {
        let context = task_get_context();

        let process_config: externs::process::PyProcessExecutionEnvironment =
            Python::with_gil(|py| process_config.bind(py).extract()).map_err(|e| format!("{e}"))?;
        let process_request = ExecuteProcess::lift(&context.core.store(), process, process_config)
            .map_err(|e| e.enrich("Error lifting Process"))
            .await?;

        let result = context.get(process_request).await?.result;

        let store = context.core.store();
        let (stdout_bytes, stderr_bytes) = try_join!(
            store
                .load_file_bytes_with(result.stdout_digest, |bytes: &[u8]| bytes.to_owned())
                .map_err(|e| e.enrich("Bytes from stdout")),
            store
                .load_file_bytes_with(result.stderr_digest, |bytes: &[u8]| bytes.to_owned())
                .map_err(|e| e.enrich("Bytes from stderr"))
        )?;

        Python::with_gil(|py| -> NodeResult<Value> {
            Ok(externs::unsafe_call(
                py,
                context.core.types.process_result,
                &[
                    externs::store_bytes(py, &stdout_bytes),
                    Snapshot::store_file_digest(py, result.stdout_digest)?,
                    externs::store_bytes(py, &stderr_bytes),
                    Snapshot::store_file_digest(py, result.stderr_digest)?,
                    externs::store_i64(py, result.exit_code.into()),
                    Snapshot::store_directory_digest(py, result.output_directory)?,
                    externs::unsafe_call(
                        py,
                        context.core.types.process_result_metadata,
                        &[
                            result
                                .metadata
                                .total_elapsed
                                .map(|d| {
                                    externs::store_u64(py, Duration::from(d).as_millis() as u64)
                                })
                                .unwrap_or_else(|| Value::from(py.None())),
                            Value::from(
                                externs::process::PyProcessExecutionEnvironment {
                                    environment: result.metadata.environment,
                                }
                                .into_pyobject(py)?,
                            ),
                            externs::store_utf8(py, result.metadata.source.into()),
                            externs::store_u64(py, result.metadata.source_run_id.0.into()),
                        ],
                    ),
                ],
            ))
        })
    })
}
