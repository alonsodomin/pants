# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from dataclasses import dataclass

from pants.backend.helm.target_types import HelmChartFieldSet
from pants.backend.helm.unittest.subsystem import HelmUnitTestPlugin
from pants.backend.helm.unittest.target_types import (
    HelmUnitTestDependenciesField,
    HelmUnitTestSourcesField,
)
from pants.backend.helm.util_rules.chart import HelmChart
from pants.backend.helm.util_rules.tool import HelmBinary
from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.core.goals.test import TestDebugRequest, TestFieldSet, TestResult, TestSubsystem
from pants.core.target_types import ResourceSourceField
from pants.core.util_rules.source_files import SourceFilesRequest
from pants.core.util_rules.stripped_source_files import StrippedSourceFiles
from pants.engine.fs import (
    AddPrefix,
    Digest,
    DigestSubset,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)
from pants.engine.process import FallibleProcessResult, Process, ProcessCacheScope
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.target import SourcesField, TransitiveTargets, TransitiveTargetsRequest
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel


class MissingUnitTestChartDependencyException(Exception):
    """Indicates that no chart has been found as dependency of the `helm_unittest_test` or
    `helm_unittest_tests` targets."""

    def __init__(self, address) -> None:
        super().__init__(
            f"Target at '{address}' needs to have a dependency on a `helm_chart` target."
        )


@dataclass(frozen=True)
class HelmUnitTestFieldSet(TestFieldSet):
    required_fields = (HelmUnitTestSourcesField,)

    sources: HelmUnitTestSourcesField
    dependencies: HelmUnitTestDependenciesField


@rule(desc="Run Helm Unittest", level=LogLevel.DEBUG)
async def run_helm_unittest(
    field_set: HelmUnitTestFieldSet,
    test_subsystem: TestSubsystem,
    helm: HelmBinary,
    plugin: HelmUnitTestPlugin,
) -> TestResult:
    transitive_targets = await Get(TransitiveTargets, TransitiveTargetsRequest([field_set.address]))
    transitive_charts = [
        target
        for target in transitive_targets.dependencies
        if HelmChartFieldSet.is_applicable(target)
    ]
    if len(transitive_charts) == 0:
        raise MissingUnitTestChartDependencyException(field_set.address)

    chart = await Get(HelmChart, HelmChartFieldSet, HelmChartFieldSet.create(transitive_charts[0]))

    source_files = await Get(
        StrippedSourceFiles,
        SourceFilesRequest(
            sources_fields=[
                field_set.sources,
                *(
                    tgt.get(SourcesField)
                    for tgt in transitive_targets.dependencies
                    if not HelmChartFieldSet.is_applicable(tgt)
                ),
            ],
            for_sources_types=(HelmUnitTestSourcesField, ResourceSourceField),
            enable_codegen=True,
        ),
    )
    prefixed_test_files_digest = await Get(
        Digest, AddPrefix(source_files.snapshot.digest, chart.path)
    )

    reports_dir = "__reports_dir"
    reports_file = f"{reports_dir}/{field_set.address.path_safe_spec}.xml"

    input_digest = await Get(
        Digest, MergeDigests([chart.snapshot.digest, prefixed_test_files_digest])
    )

    # Cache test runs only if they are successful, or not at all if `--test-force`.
    cache_scope = (
        ProcessCacheScope.PER_SESSION if test_subsystem.force else ProcessCacheScope.SUCCESSFUL
    )

    process_result = await Get(
        FallibleProcessResult,
        Process,
        helm.run_plugin(
            plugin,
            [
                "--helm3",
                "--output-type",
                plugin.output_type.value,
                "--output-file",
                reports_file,
                chart.path,
            ],
            description=f"Running Helm unittest on: {field_set.address}",
            input_digest=input_digest,
            cache_scope=cache_scope,
            output_dirs=(reports_dir,),
        ),
    )

    xml_result_subset = await Get(
        Digest,
        DigestSubset(
            process_result.output_digest,
            PathGlobs(
                [f"{reports_dir}/**"],
                glob_match_error_behavior=GlobMatchErrorBehavior.error,
                description_of_origin="`test` goal",
            ),
        ),
    )
    xml_results = await Get(Snapshot, RemovePrefix(xml_result_subset, reports_dir))

    return TestResult.from_fallible_process_result(
        process_result,
        address=field_set.address,
        output_setting=test_subsystem.output,
        xml_results=xml_results,
    )


@rule
async def generate_helm_unittest_debug_request(field_set: HelmUnitTestFieldSet) -> TestDebugRequest:
    raise NotImplementedError("This is a stub")


def rules():
    return [*collect_rules(), UnionRule(TestFieldSet, HelmUnitTestFieldSet)]
