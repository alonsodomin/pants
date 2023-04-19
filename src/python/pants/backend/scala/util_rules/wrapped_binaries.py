# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from dataclasses import dataclass

from pants.backend.scala.util_rules import versions
from pants.backend.scala.util_rules.versions import (
    ScalaArtifactsForVersionRequest,
    ScalaArtifactsForVersionResult,
)
from pants.engine.fs import Digest
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.unions import UnionRule
from pants.jvm import wrapped_binaries
from pants.jvm.resolve.common import ArtifactRequirements
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool
from pants.jvm.wrapped_binaries import (
    CompileJvmWrappedBinaryRequest,
    ResolvedJvmCompiler,
    ResolveJvmCompilerRequest,
)


@dataclass(frozen=True)
class ResolveScalaCompilerRequest(ResolveJvmCompilerRequest):
    scala_version: str


@rule
async def resolve_scala_compiler(request: ResolveScalaCompilerRequest) -> ResolvedJvmCompiler:
    resolved_artifacts = await Get(
        ScalaArtifactsForVersionResult, ScalaArtifactsForVersionRequest(request.scala_version)
    )
    classpath = await Get(
        ToolClasspath,
        ToolClasspathRequest(
            artifact_requirements=ArtifactRequirements.from_coordinates(
                resolved_artifacts.all_coordinates
            )
        ),
    )
    return ResolvedJvmCompiler(
        compiler_main=resolved_artifacts.compiler_main,
        classpath_entries=tuple(classpath.classpath_entries()),
        digest=classpath.digest,
    )


class CompileScalaWrappedBinaryRequest:
    @staticmethod
    def create(
        *,
        name: str,
        sources: Digest,
        lockfile_request: GenerateJvmLockfileFromTool,
        scala_version: str,
    ) -> CompileJvmWrappedBinaryRequest:
        return CompileJvmWrappedBinaryRequest(
            name=name,
            sources=sources,
            accepted_file_extensions=(".scala",),
            lockfile_request=lockfile_request,
            compiler_request=ResolveScalaCompilerRequest(scala_version),
        )


def rules():
    return [
        *collect_rules(),
        *versions.rules(),
        *wrapped_binaries.rules(),
        UnionRule(ResolveJvmCompilerRequest, ResolveScalaCompilerRequest),
    ]
