# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from dataclasses import dataclass

from pants.engine.fs import Digest
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.unions import UnionRule
from pants.jvm import wrapped_binaries
from pants.jvm.resolve.common import ArtifactRequirements, Coordinate
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool
from pants.jvm.wrapped_binaries import (
    CompileJvmWrappedBinaryRequest,
    ResolvedJvmCompiler,
    ResolveJvmCompilerRequest,
)


@dataclass(frozen=True)
class ResolveKotlinCompilerRequest(ResolveJvmCompilerRequest):
    kotlin_version: str


@rule
async def resolve_kotlin_compiler(request: ResolveKotlinCompilerRequest) -> ResolvedJvmCompiler:
    classpath = await Get(
        ToolClasspath,
        ToolClasspathRequest(
            artifact_requirements=ArtifactRequirements.from_coordinates(
                [
                    Coordinate(
                        group="org.jetbrains.kotlin",
                        artifact="kotlin-compiler-embeddable",
                        version=request.kotlin_version,
                    )
                ]
            )
        ),
    )
    return ResolvedJvmCompiler(
        compiler_main="org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
        classpath_entries=tuple(classpath.classpath_entries()),
        digest=classpath.digest,
    )


class CompileKotlinWrappedBinaryRequest:
    @staticmethod
    def create(
        *,
        name: str,
        sources: Digest,
        lockfile_request: GenerateJvmLockfileFromTool,
        kotlin_version: str,
    ) -> CompileJvmWrappedBinaryRequest:
        return CompileJvmWrappedBinaryRequest(
            name=name,
            sources=sources,
            accepted_file_extensions=(".kt",),
            lockfile_request=lockfile_request,
            compiler_request=ResolveKotlinCompilerRequest(kotlin_version),
        )


def rules():
    return [
        *collect_rules(),
        *wrapped_binaries.rules(),
        UnionRule(ResolveJvmCompilerRequest, ResolveKotlinCompilerRequest),
    ]
