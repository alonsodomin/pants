# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from pants.backend.helm.target_types import HelmChartFieldSet, HelmChartOutputPathField
from pants.backend.helm.util_rules.chart import HelmChartRequest, get_helm_chart
from pants.backend.helm.util_rules.chart_metadata import HelmChartMetadata
from pants.backend.helm.util_rules.tool import HelmProcess
from pants.core.goals.package import BuiltPackage, BuiltPackageArtifact, PackageFieldSet
from pants.engine.fs import AddPrefix, CreateDigest, Directory, RemovePrefix
from pants.engine.intrinsics import create_digest, digest_to_snapshot, remove_prefix
from pants.engine.process import execute_process_or_raise
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.unions import UnionRule
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuiltHelmArtifact(BuiltPackageArtifact):
    info: HelmChartMetadata | None = None

    @classmethod
    def create(cls, relpath: str, info: HelmChartMetadata) -> BuiltHelmArtifact:
        return cls(
            relpath=relpath,
            info=info,
            extra_log_lines=(f"Built Helm chart artifact: {relpath}",),
        )


@dataclass(frozen=True)
class HelmPackageFieldSet(HelmChartFieldSet, PackageFieldSet):
    output_path: HelmChartOutputPathField


@rule(desc="Package Helm chart", level=LogLevel.DEBUG)
async def run_helm_package(field_set: HelmPackageFieldSet) -> BuiltPackage:
    result_dir = "__out"

    chart, result_digest = await concurrently(
        get_helm_chart(HelmChartRequest(field_set), **implicitly()),
        create_digest(CreateDigest([Directory(result_dir)])),
    )

    process_output_file = os.path.join(result_dir, f"{chart.info.artifact_name}.tgz")

    process_result = await execute_process_or_raise(
        **implicitly(
            HelmProcess(
                argv=["package", chart.name, "-d", result_dir],
                input_digest=result_digest,
                extra_immutable_input_digests=chart.immutable_input_digests,
                output_files=(process_output_file,),
                description=f"Packaging Helm chart: {field_set.address}",
            )
        )
    )

    stripped_output_digest = await remove_prefix(
        RemovePrefix(process_result.output_digest, result_dir)
    )

    final_snapshot = await digest_to_snapshot(
        **implicitly(
            AddPrefix(
                stripped_output_digest, field_set.output_path.value_or_default(file_ending=None)
            )
        )
    )
    return BuiltPackage(
        final_snapshot.digest,
        artifacts=tuple(
            BuiltHelmArtifact.create(file, chart.info) for file in final_snapshot.files
        ),
    )


def rules():
    return [*collect_rules(), UnionRule(PackageFieldSet, HelmPackageFieldSet)]
