# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

from pants.backend.javascript import install_node_package, nodejs_project_environment
from pants.backend.javascript.install_node_package import (
    InstalledNodePackageRequest,
    install_node_packages_for_address,
)
from pants.backend.javascript.nodejs_project_environment import (
    NodeJsProjectEnvironmentProcess,
    setup_nodejs_project_environment_process,
)
from pants.backend.javascript.package_json import (
    NodePackageNameField,
    NodePackageTestScriptField,
    NodeTestScript,
    OwningNodePackageRequest,
    find_owning_package,
)
from pants.backend.javascript.subsystems.nodejstest import NodeJSTest
from pants.backend.javascript.target_types import JSRuntimeSourceField, JSTestRuntimeSourceField
from pants.backend.typescript.target_types import TypeScriptSourceField
from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.build_graph.address import Address
from pants.core.goals.test import (
    CoverageData,
    CoverageDataCollection,
    CoverageReports,
    FilesystemCoverageReport,
    TestExtraEnv,
    TestExtraEnvVarsField,
    TestFieldSet,
    TestRequest,
    TestResult,
    TestsBatchCompatibilityTagField,
    TestSubsystem,
    TestTimeoutField,
)
from pants.core.target_types import AssetSourceField
from pants.core.util_rules import source_files
from pants.core.util_rules.distdir import DistDir
from pants.core.util_rules.partitions import Partition, PartitionerType, Partitions
from pants.core.util_rules.source_files import SourceFilesRequest, determine_source_files
from pants.engine.env_vars import EnvironmentVarsRequest
from pants.engine.fs import DigestSubset, GlobExpansionConjunction
from pants.engine.internals import graph, platform_rules
from pants.engine.internals.graph import transitive_targets
from pants.engine.internals.native_engine import MergeDigests, Snapshot
from pants.engine.internals.platform_rules import environment_vars_subset
from pants.engine.internals.selectors import concurrently
from pants.engine.intrinsics import digest_to_snapshot, merge_digests
from pants.engine.process import ProcessCacheScope, ProcessWithRetries, execute_process_with_retry
from pants.engine.rules import Rule, collect_rules, implicitly, rule
from pants.engine.target import Dependencies, SourcesField, Target, TransitiveTargetsRequest
from pants.engine.unions import UnionRule
from pants.util.dirutil import fast_relpath
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.strutil import pluralize


@dataclass(frozen=True)
class JSCoverageData(CoverageData):
    snapshot: Snapshot
    addresses: tuple[Address, ...]
    output_files: tuple[str, ...]
    output_directories: tuple[str, ...]
    working_directory: str


class JSCoverageDataCollection(CoverageDataCollection[JSCoverageData]):
    element_type = JSCoverageData


@dataclass(frozen=True)
class JSTestFieldSet(TestFieldSet):
    required_fields = (JSTestRuntimeSourceField,)

    batch_compatibility_tag: TestsBatchCompatibilityTagField
    source: JSTestRuntimeSourceField
    dependencies: Dependencies
    timeout: TestTimeoutField
    extra_env_vars: TestExtraEnvVarsField


class JSTestRequest(TestRequest):
    tool_subsystem = NodeJSTest
    field_set_type = JSTestFieldSet

    partitioner_type = PartitionerType.CUSTOM


@dataclass(frozen=True)
class TestMetadata:
    extra_env_vars: tuple[str, ...]
    owning_target: Target
    compatibility_tag: str | None = None

    __test__ = False

    @property
    def description(self) -> str:
        return f"{self.owning_target[NodePackageNameField].value} {self.compatibility_tag or ''}"


@rule(desc="Partition NodeJS tests", level=LogLevel.DEBUG)
async def partition_nodejs_tests(
    request: JSTestRequest.PartitionRequest[JSTestFieldSet],
) -> Partitions[JSTestFieldSet, TestMetadata]:
    partitions = []
    compatible_tests = defaultdict(list)
    owning_packages = await concurrently(
        find_owning_package(OwningNodePackageRequest(field_set.address))
        for field_set in request.field_sets
    )
    for field_set, owning_package in zip(request.field_sets, owning_packages):
        metadata = TestMetadata(
            extra_env_vars=field_set.extra_env_vars.sorted(),
            owning_target=owning_package.ensure_owner(),
            compatibility_tag=field_set.batch_compatibility_tag.value,
        )

        if not metadata.compatibility_tag:
            partitions.append(Partition((field_set,), metadata))
        else:
            compatible_tests[metadata].append(field_set)

    for metadata, field_sets in compatible_tests.items():
        partitions.append(Partition(tuple(field_sets), metadata))

    return Partitions(partitions)


@rule(level=LogLevel.DEBUG, desc="Run javascript tests")
async def run_javascript_tests(
    batch: JSTestRequest.Batch[JSTestFieldSet, TestMetadata],
    test: TestSubsystem,
    test_extra_env: TestExtraEnv,
) -> TestResult:
    field_sets = batch.elements
    metadata = batch.partition_metadata
    installation_get = install_node_packages_for_address(
        InstalledNodePackageRequest(metadata.owning_target.address), **implicitly()
    )
    transitive_tgts_get = transitive_targets(
        TransitiveTargetsRequest(field_set.address for field_set in field_sets), **implicitly()
    )

    field_set_source_files_get = determine_source_files(
        SourceFilesRequest(field_set.source for field_set in field_sets)
    )
    target_env_vars_get = environment_vars_subset(
        EnvironmentVarsRequest(metadata.extra_env_vars), **implicitly()
    )
    installation, transitive_tgts, field_set_source_files, target_env_vars = await concurrently(
        installation_get, transitive_tgts_get, field_set_source_files_get, target_env_vars_get
    )

    sources = await determine_source_files(
        SourceFilesRequest(
            (tgt.get(SourcesField) for tgt in transitive_tgts.closure),
            enable_codegen=True,
            for_sources_types=[
                JSRuntimeSourceField,
                TypeScriptSourceField,
                AssetSourceField,
            ],
        )
    )
    merged_digest = await merge_digests(
        MergeDigests([sources.snapshot.digest, installation.digest])
    )

    def relative_package_dir(file: str) -> str:
        return fast_relpath(file, installation.project_env.package_dir())

    test_script = installation.project_env.ensure_target()[NodePackageTestScriptField].value
    entry_point = test_script.entry_point

    coverage_args: tuple[str, ...] = ()
    output_files: list[str] = []
    output_directories: list[str] = []
    if test.use_coverage and test_script.supports_coverage():
        coverage_args = test_script.coverage_args
        output_files.extend(test_script.coverage_output_files)
        output_directories.extend(test_script.coverage_output_directories)
        entry_point = test_script.coverage_entry_point or entry_point

    timeout_seconds: int | None = None
    for field_set in field_sets:
        timeout = field_set.timeout.calculate_from_global_options(test)
        if timeout:
            if timeout_seconds:
                timeout_seconds += timeout
            else:
                timeout_seconds = timeout
    file_description = field_sets[0].address.spec
    if len(field_sets) > 1:
        file_description += f"+ {pluralize(len(field_sets) - 1, 'other file')}"
    process = await setup_nodejs_project_environment_process(
        NodeJsProjectEnvironmentProcess(
            installation.project_env,
            args=(
                "run",
                entry_point,
                *installation.project_env.project.args_separator,
                *sorted(relative_package_dir(file) for file in field_set_source_files.files),
                *coverage_args,
            ),
            description=f"Running npm tests for {file_description}.",
            input_digest=merged_digest,
            level=LogLevel.INFO,
            extra_env=FrozenDict(**test_extra_env.env, **target_env_vars),
            timeout_seconds=timeout_seconds,
            output_files=tuple(
                installation.join_relative_workspace_directory(file) for file in output_files or ()
            ),
            output_directories=tuple(
                installation.join_relative_workspace_directory(directory)
                for directory in output_directories or ()
            ),
        ),
        **implicitly(),
    )
    if test.force:
        process = dataclasses.replace(process, cache_scope=ProcessCacheScope.PER_SESSION)

    results = await execute_process_with_retry(ProcessWithRetries(process, test.attempts_default))
    coverage_data: JSCoverageData | None = None
    if test.use_coverage:
        coverage_snapshot = await digest_to_snapshot(
            **implicitly(
                DigestSubset(
                    results.last.output_digest,
                    test_script.coverage_globs(
                        installation.project_env.relative_workspace_directory()
                    ),
                )
            )
        )

        coverage_data = JSCoverageData(
            coverage_snapshot,
            tuple(field_set.address for field_set in field_sets),
            output_files=test_script.coverage_output_files,
            output_directories=test_script.coverage_output_directories,
            working_directory=installation.project_env.relative_workspace_directory(),
        )

    return TestResult.from_batched_fallible_process_result(
        results.results, batch, test.output, coverage_data=coverage_data
    )


@rule(desc="Collecting coverage reports.")
async def collect_coverage_reports(
    coverage_reports: JSCoverageDataCollection,
    dist_dir: DistDir,
    nodejs_test: NodeJSTest,
) -> CoverageReports:
    gets_per_data = [
        (
            file,
            report,
            digest_to_snapshot(
                **implicitly(
                    DigestSubset(
                        report.snapshot.digest,
                        NodeTestScript.coverage_globs_for(
                            report.working_directory,
                            (file,),
                            report.output_directories,
                            GlobMatchErrorBehavior.error,
                            GlobExpansionConjunction.all_match,
                            description_of_origin="the JS coverage report collection rule",
                        ),
                    )
                )
            ),
        )
        for report in coverage_reports
        for file in report.output_files
    ]
    snapshots = await concurrently(get for _, _, get in gets_per_data)
    return CoverageReports(
        tuple(
            _get_report(
                nodejs_test, dist_dir, snapshot, data.addresses, file, data.working_directory
            )
            for (file, data), snapshot in zip(
                ((file, report) for file, report, _ in gets_per_data), snapshots
            )
        )
    )


def _get_report(
    nodejs_test: NodeJSTest,
    dist_dir: DistDir,
    snapshot: Snapshot,
    addresses: tuple[Address, ...],
    file: str,
    working_directory: str,
) -> FilesystemCoverageReport:
    # It is up to the user to configure the output coverage reports.
    file_path = PurePath(file)
    output_dir = nodejs_test.render_coverage_output_dir(dist_dir, addresses)
    return FilesystemCoverageReport(
        coverage_insufficient=False,
        result_snapshot=snapshot,
        directory_to_materialize_to=output_dir,
        report_file=output_dir / working_directory / file_path,
        report_type=file_path.suffix,
    )


def rules() -> Iterable[Rule | UnionRule]:
    return [
        *platform_rules.rules(),
        *graph.rules(),
        *nodejs_project_environment.rules(),
        *install_node_package.rules(),
        *source_files.rules(),
        *JSTestRequest.rules(),
        UnionRule(CoverageDataCollection, JSCoverageDataCollection),
        *collect_rules(),
    ]
