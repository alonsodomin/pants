# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from abc import ABC
from dataclasses import dataclass

from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.engine.environment import EnvironmentName
from pants.engine.fs import (
    EMPTY_DIGEST,
    CreateDigest,
    Digest,
    DigestEntries,
    DigestSubset,
    Directory,
    FileEntry,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)
from pants.engine.process import ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.unions import UnionRule, union
from pants.jvm.compile import ClasspathEntry
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool
from pants.util.logging import LogLevel


@union(in_scope_types=[EnvironmentName])
@dataclass(frozen=True)
class ResolveJvmCompilerRequest(ABC):
    pass


@dataclass(frozen=True)
class ResolvedJvmCompiler:
    compiler_main: str
    classpath_entries: tuple[str, ...]
    digest: Digest = EMPTY_DIGEST


@dataclass(frozen=True)
class CompileJvmWrappedBinaryRequest:
    name: str
    sources: Digest
    accepted_file_extensions: tuple[str, ...]
    lockfile_request: GenerateJvmLockfileFromTool
    compiler_request: ResolveJvmCompilerRequest


@rule
async def compile_jvm_wrapped_binary(
    request: CompileJvmWrappedBinaryRequest, jdk: InternalJdk
) -> ClasspathEntry:
    dest_dir = "classfiles"

    resolved_compiler, binary_classpath, sources_subset, empty_dest_dir = await MultiGet(
        Get(ResolvedJvmCompiler, ResolveJvmCompilerRequest, request.compiler_request),
        Get(
            ToolClasspath,
            ToolClasspathRequest(prefix="__toolcp", lockfile=request.lockfile_request),
        ),
        Get(
            Digest,
            DigestSubset(
                request.sources,
                PathGlobs(
                    [f"**/*{ext}" for ext in request.accepted_file_extensions],
                    glob_match_error_behavior=GlobMatchErrorBehavior.error,
                    description_of_origin=f"{request.name} sources",
                ),
            ),
        ),
        Get(Digest, CreateDigest([Directory(path=dest_dir)])),
    )

    merged_digest, source_entries = await MultiGet(
        Get(
            Digest,
            MergeDigests(
                [binary_classpath.digest, resolved_compiler.digest, request.sources, empty_dest_dir]
            ),
        ),
        Get(DigestEntries, Digest, sources_subset),
    )

    compile_result = await Get(
        ProcessResult,
        JvmProcess(
            jdk=jdk,
            classpath_entries=resolved_compiler.classpath_entries,
            argv=[
                resolved_compiler.compiler_main,
                "-classpath",
                ":".join(binary_classpath.classpath_entries()),
                "-d",
                dest_dir,
                *[entry.path for entry in source_entries if isinstance(entry, FileEntry)],
            ],
            input_digest=merged_digest,
            output_directories=(dest_dir,),
            description=f"Compile {request.name} sources.",
            level=LogLevel.DEBUG,
            use_nailgun=False,
        ),
    )

    stripped_classfiles_snapshot = await Get(
        Snapshot, RemovePrefix(compile_result.output_digest, dest_dir)
    )
    return ClasspathEntry(
        digest=stripped_classfiles_snapshot.digest, filenames=stripped_classfiles_snapshot.files
    )


class ResolveJavaCompilerRequest(ResolveJvmCompilerRequest):
    pass


class CompileJavaWrappedBinaryRequest:
    @staticmethod
    def create(
        *, name: str, sources: Digest, lockfile_request: GenerateJvmLockfileFromTool
    ) -> CompileJvmWrappedBinaryRequest:
        return CompileJvmWrappedBinaryRequest(
            name=name,
            sources=sources,
            accepted_file_extensions=(".java",),
            lockfile_request=lockfile_request,
            compiler_request=ResolveJavaCompilerRequest(),
        )


@rule
def resolve_java_compiler(_: ResolveJavaCompilerRequest, jdk: InternalJdk) -> ResolvedJvmCompiler:
    return ResolvedJvmCompiler(
        compiler_main="com.sun.tools.javac.Main",
        classpath_entries=(f"{jdk.java_home}/lib/tools.jar",),
    )


def rules():
    return [
        *collect_rules(),
        UnionRule(ResolveJvmCompilerRequest, ResolveJavaCompilerRequest),
    ]
