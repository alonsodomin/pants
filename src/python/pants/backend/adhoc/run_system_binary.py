# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from io import StringIO

from pants.backend.adhoc.target_types import (
    SystemBinaryExtraSearchPathsField,
    SystemBinaryFingerprintArgsField,
    SystemBinaryFingerprintDependenciesField,
    SystemBinaryFingerprintPattern,
    SystemBinaryLogFingerprintingErrorsField,
    SystemBinaryNameField,
)
from pants.build_graph.address import Address
from pants.core.goals.run import RunFieldSet, RunInSandboxBehavior, RunRequest
from pants.core.util_rules.adhoc_process_support import (
    ResolveExecutionDependenciesRequest,
    resolve_execution_environment,
)
from pants.core.util_rules.system_binaries import (
    BinaryPath,
    BinaryPathRequest,
    SearchPath,
    SystemBinariesSubsystem,
    find_binary,
)
from pants.engine.internals.native_engine import EMPTY_DIGEST, Digest
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import execute_process
from pants.engine.process import FallibleProcessResult, Process
from pants.engine.rules import collect_rules, implicitly, rule
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SystemBinaryFieldSet(RunFieldSet):
    run_in_sandbox_behavior = RunInSandboxBehavior.RUN_REQUEST_HERMETIC

    required_fields = (
        SystemBinaryNameField,
        SystemBinaryExtraSearchPathsField,
        SystemBinaryFingerprintPattern,
        SystemBinaryFingerprintArgsField,
        SystemBinaryFingerprintDependenciesField,
        SystemBinaryLogFingerprintingErrorsField,
    )

    name: SystemBinaryNameField
    extra_search_paths: SystemBinaryExtraSearchPathsField
    fingerprint_pattern: SystemBinaryFingerprintPattern
    fingerprint_argv: SystemBinaryFingerprintArgsField
    fingerprint_dependencies: SystemBinaryFingerprintDependenciesField
    log_fingerprinting_errors: SystemBinaryLogFingerprintingErrorsField


async def _find_binary(
    address: Address,
    binary_name: str,
    search_path: SearchPath,
    fingerprint_pattern: str | None,
    fingerprint_args: tuple[str, ...] | None,
    fingerprint_dependencies: tuple[str, ...] | None,
    log_fingerprinting_errors: bool,
) -> BinaryPath:
    binaries = await find_binary(
        BinaryPathRequest(
            binary_name=binary_name,
            search_path=search_path,
        ),
        **implicitly(),
    )

    fingerprint_args = fingerprint_args or ()

    deps = await resolve_execution_environment(
        ResolveExecutionDependenciesRequest(address, (), fingerprint_dependencies), **implicitly()
    )
    rds = deps.runnable_dependencies
    env: dict[str, str] = {}
    append_only_caches: Mapping[str, str] = {}
    immutable_input_digests: Mapping[str, Digest] = {}
    if rds:
        env = {"PATH": rds.path_component}
        env.update(**(rds.extra_env or {}))
        append_only_caches = rds.append_only_caches
        immutable_input_digests = rds.immutable_input_digests

    tests: tuple[FallibleProcessResult, ...] = await concurrently(
        execute_process(
            Process(
                description=f"Testing candidate for `{binary_name}` at `{path.path}`",
                argv=(path.path,) + fingerprint_args,
                input_digest=deps.digest,
                env=env,
                append_only_caches=append_only_caches,
                immutable_input_digests=immutable_input_digests,
            ),
            **implicitly(),
        )
        for path in binaries.paths
    )

    for test, binary in zip(tests, binaries.paths):
        if test.exit_code != 0:
            if log_fingerprinting_errors:
                logger.warning(
                    f"Error occurred while fingerprinting candidate binary `{binary.path}` "
                    f"for binary `{binary_name}` (exit code {test.exit_code}) (use the `{SystemBinaryLogFingerprintingErrorsField.alias}` field to control this warning):\n\n"
                    f"stdout:\n{test.stdout.decode(errors='ignore')}\n"
                    f"stderr:\n{test.stderr.decode(errors='ignore')}"
                )

            # Skip this binary since fingerprinting failed.
            continue

        if fingerprint_pattern:
            fingerprint = test.stdout.decode().strip()
            match = re.match(fingerprint_pattern, fingerprint)
            if not match:
                continue

        return binary

    message = StringIO()
    message.write(f"Could not find a binary with name `{binary_name}`")
    if fingerprint_pattern:
        message.write(
            f" with output matching `{fingerprint_pattern}` when run with arguments `{' '.join(fingerprint_args or ())}`"
        )

    message.write(". The following paths were searched:\n")
    for sp in search_path:
        message.write(f"- {sp}\n")

    failed_tests = [
        (test, binary) for test, binary in zip(tests, binaries.paths) if test.exit_code != 0
    ]
    if failed_tests:
        message.write(
            "\n\nThe following binaries were skipped because each binary returned an error when invoked:"
        )
        for failed_test, failed_binary in failed_tests:
            message.write(f"\n\n- {failed_binary.path} (exit code {failed_test.exit_code})\n")
            message.write(f"  stdout:\n{failed_test.stdout.decode(errors='ignore')}\n")
            message.write(f"  stderr:\n{failed_test.stderr.decode(errors='ignore')}\n")

    raise ValueError(message.getvalue())


@rule(level=LogLevel.DEBUG)
async def create_system_binary_run_request(
    field_set: SystemBinaryFieldSet,
    system_binaries: SystemBinariesSubsystem.EnvironmentAware,
) -> RunRequest:
    assert field_set.name.value is not None
    extra_search_paths = field_set.extra_search_paths.value or ()

    search_path = SearchPath((*extra_search_paths, *system_binaries.system_binary_paths))

    path = await _find_binary(
        field_set.address,
        field_set.name.value,
        search_path,
        field_set.fingerprint_pattern.value,
        field_set.fingerprint_argv.value,
        field_set.fingerprint_dependencies.value,
        field_set.log_fingerprinting_errors.value,
    )

    return RunRequest(
        digest=EMPTY_DIGEST,
        args=[path.path],
    )


def rules():
    return [
        *collect_rules(),
        *SystemBinaryFieldSet.rules(),
    ]
