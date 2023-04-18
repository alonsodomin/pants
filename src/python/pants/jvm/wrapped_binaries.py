# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.engine.fs import (
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
from pants.jvm.compile import ClasspathEntry
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.resolve.common import ArtifactRequirements, Coordinate
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class CompileJvmWrappedBinaryRequest:
    tool_name: str
    sources: Digest
    accepted_file_extensions: tuple[str, ...]
    lockfile_request: GenerateJvmLockfileFromTool
    compiler_classname: str
    compiler_requirements: ArtifactRequirements | None

    @classmethod
    def for_java_sources(
        cls, *, tool_name: str, sources: Digest, lockfile_request: GenerateJvmLockfileFromTool
    ) -> CompileJvmWrappedBinaryRequest:
        return cls(
            tool_name=tool_name,
            sources=sources,
            accepted_file_extensions=(".java",),
            lockfile_request=lockfile_request,
            compiler_classname="com.sun.tools.javac.Main",
            compiler_requirements=None,
        )

    @classmethod
    def for_scala_sources(
        cls,
        *,
        tool_name: str,
        sources: Digest,
        lockfile_request: GenerateJvmLockfileFromTool,
        scala_version: str,
    ) -> CompileJvmWrappedBinaryRequest:
        return cls(
            tool_name=tool_name,
            sources=sources,
            accepted_file_extensions=(".scala",),
            lockfile_request=lockfile_request,
            compiler_classname="scala.tools.nsc.Main",
            compiler_requirements=ArtifactRequirements.from_coordinates(
                [
                    Coordinate(
                        group="org.scala-lang",
                        artifact="scala-compiler",
                        version=scala_version,
                    ),
                    Coordinate(
                        group="org.scala-lang",
                        artifact="scala-library",
                        version=scala_version,
                    ),
                    Coordinate(
                        group="org.scala-lang",
                        artifact="scala-reflect",
                        version=scala_version,
                    ),
                ]
            ),
        )

    @classmethod
    def for_kotlin_sources(
        cls,
        *,
        tool_name: str,
        sources: Digest,
        lockfile_request: GenerateJvmLockfileFromTool,
        kotlin_version: str,
    ) -> CompileJvmWrappedBinaryRequest:
        return cls(
            tool_name=tool_name,
            sources=sources,
            accepted_file_extensions=(".kt",),
            lockfile_request=lockfile_request,
            compiler_classname="org.jetbrains.kotlin.cli.jvm.K2JVMCompiler",
            compiler_requirements=ArtifactRequirements.from_coordinates(
                [
                    Coordinate(
                        group="org.jetbrains.kotlin",
                        artifact="kotlin-compiler-embeddable",
                        version=kotlin_version,
                    ),
                ]
            ),
        )

class CompiledJvmWrappedBinary:
    tool_name: ClassVar[str]

@rule
async def compile_jvm_wrapped_binary(
    request: CompileJvmWrappedBinaryRequest, jdk: InternalJdk
) -> ClasspathEntry:
    dest_dir = "classfiles"

    materialized_classpath, sources_subset, empty_dest_dir = await MultiGet(
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
                    description_of_origin=f"{request.tool_name} sources",
                ),
            ),
        ),
        Get(Digest, CreateDigest([Directory(path=dest_dir)])),
    )

    input_digests = [materialized_classpath.digest, request.sources, empty_dest_dir]
    if request.compiler_requirements:
        compiler_classpath = await Get(
            ToolClasspath,
            ToolClasspathRequest(
                prefix="__compilercp", artifact_requirements=request.compiler_requirements
            ),
        )
        compiler_classpath_entries = list(compiler_classpath.classpath_entries())
        input_digests.append(compiler_classpath.digest)
    else:
        compiler_classpath_entries = [f"{jdk.java_home}/lib/tools.jar"]

    merged_digest, source_entries = await MultiGet(
        Get(Digest, MergeDigests(input_digests)),
        Get(DigestEntries, Digest, sources_subset),
    )

    compile_result = await Get(
        ProcessResult,
        JvmProcess(
            jdk=jdk,
            classpath_entries=compiler_classpath_entries,
            argv=[
                request.compiler_classname,
                "-classpath",
                ":".join(materialized_classpath.classpath_entries()),
                "-d",
                dest_dir,
                *[entry.path for entry in source_entries if isinstance(entry, FileEntry)],
            ],
            input_digest=merged_digest,
            output_directories=(dest_dir,),
            description=f"Compile {request.tool_name} sources.",
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


def rules():
    return collect_rules()
