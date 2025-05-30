# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import importlib.resources
import json
import logging
import os.path
from dataclasses import dataclass

from pants.backend.java.dependency_inference.types import JavaSourceDependencyAnalysis
from pants.core.goals.resolves import ExportableTool
from pants.core.util_rules.source_files import SourceFiles
from pants.engine.fs import AddPrefix, CreateDigest, Digest, Directory, FileContent
from pants.engine.internals.native_engine import MergeDigests, RemovePrefix
from pants.engine.intrinsics import (
    add_prefix,
    create_digest,
    execute_process,
    get_digest_contents,
    merge_digests,
    remove_prefix,
)
from pants.engine.process import (
    FallibleProcessResult,
    ProductDescription,
    execute_process_or_raise,
    fallible_to_exec_result_or_raise,
)
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.unions import UnionRule
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspathRequest, materialize_classpath_for_tool
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, JvmToolBase
from pants.util.logging import LogLevel

logger = logging.getLogger(__name__)


_LAUNCHER_BASENAME = "PantsJavaParserLauncher.java"


class JavaParser(JvmToolBase):
    options_scope = "java-parser"
    help = "Internal tool for parsing JVM sources to identify dependencies"

    default_artifacts = (
        "com.fasterxml.jackson.core:jackson-databind:2.12.4",
        "com.fasterxml.jackson.datatype:jackson-datatype-jdk8:2.12.4",
        "com.github.javaparser:javaparser-symbol-solver-core:3.25.5",
    )
    default_lockfile_resource = (
        "pants.backend.java.dependency_inference",
        "java_parser.lock",
    )


@dataclass(frozen=True)
class JavaSourceDependencyAnalysisRequest:
    source_files: SourceFiles


@dataclass(frozen=True)
class FallibleJavaSourceDependencyAnalysisResult:
    process_result: FallibleProcessResult


@dataclass(frozen=True)
class JavaParserCompiledClassfiles:
    digest: Digest


@rule(level=LogLevel.DEBUG)
async def resolve_fallible_result_to_analysis(
    fallible_result: FallibleJavaSourceDependencyAnalysisResult,
) -> JavaSourceDependencyAnalysis:
    desc = ProductDescription("Java source dependency analysis failed.")
    result = await fallible_to_exec_result_or_raise(
        **implicitly(
            {fallible_result.process_result: FallibleProcessResult, desc: ProductDescription}
        )
    )
    analysis_contents = await get_digest_contents(result.output_digest)
    analysis = json.loads(analysis_contents[0].content)
    return JavaSourceDependencyAnalysis.from_json_dict(analysis)


@rule(level=LogLevel.DEBUG)
async def make_analysis_request_from_source_files(
    source_files: SourceFiles,
) -> JavaSourceDependencyAnalysisRequest:
    return JavaSourceDependencyAnalysisRequest(source_files=source_files)


@rule(level=LogLevel.DEBUG)
async def analyze_java_source_dependencies(
    processor_classfiles: JavaParserCompiledClassfiles,
    jdk: InternalJdk,
    tool: JavaParser,
    request: JavaSourceDependencyAnalysisRequest,
) -> FallibleJavaSourceDependencyAnalysisResult:
    source_files = request.source_files
    if len(source_files.files) > 1:
        raise ValueError(
            f"parse_java_package expects sources with exactly 1 source file, but found {len(source_files.files)}."
        )
    elif len(source_files.files) == 0:
        raise ValueError(
            "parse_java_package expects sources with exactly 1 source file, but found none."
        )
    source_prefix = "__source_to_analyze"
    source_path = os.path.join(source_prefix, source_files.files[0])
    processorcp_relpath = "__processorcp"
    toolcp_relpath = "__toolcp"

    tool_classpath, prefixed_source_files_digest = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(lockfile=(GenerateJvmLockfileFromTool.create(tool)))
        ),
        add_prefix(AddPrefix(source_files.snapshot.digest, source_prefix)),
    )

    extra_immutable_input_digests = {
        toolcp_relpath: tool_classpath.digest,
        processorcp_relpath: processor_classfiles.digest,
    }

    analysis_output_path = "__source_analysis.json"

    process_result = await execute_process(
        **implicitly(
            JvmProcess(
                jdk=jdk,
                classpath_entries=[
                    *tool_classpath.classpath_entries(toolcp_relpath),
                    processorcp_relpath,
                ],
                argv=[
                    "org.pantsbuild.javaparser.PantsJavaParserLauncher",
                    analysis_output_path,
                    source_path,
                ],
                input_digest=prefixed_source_files_digest,
                extra_immutable_input_digests=extra_immutable_input_digests,
                output_files=(analysis_output_path,),
                extra_nailgun_keys=extra_immutable_input_digests,
                description=f"Analyzing {source_files.files[0]}",
                level=LogLevel.DEBUG,
            )
        )
    )

    return FallibleJavaSourceDependencyAnalysisResult(process_result=process_result)


def _load_javaparser_launcher_source() -> bytes:
    parent_module = ".".join(__name__.split(".")[:-1])
    return importlib.resources.files(parent_module).joinpath(_LAUNCHER_BASENAME).read_bytes()


# TODO(13879): Consolidate compilation of wrapper binaries to common rules.
@rule
async def build_processors(jdk: InternalJdk, tool: JavaParser) -> JavaParserCompiledClassfiles:
    dest_dir = "classfiles"
    materialized_classpath, source_digest = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(
                prefix="__toolcp", lockfile=GenerateJvmLockfileFromTool.create(tool)
            )
        ),
        create_digest(
            CreateDigest(
                [
                    FileContent(
                        path=_LAUNCHER_BASENAME,
                        content=_load_javaparser_launcher_source(),
                    ),
                    Directory(dest_dir),
                ]
            )
        ),
    )

    merged_digest = await merge_digests(
        MergeDigests(
            (
                materialized_classpath.digest,
                source_digest,
            )
        )
    )

    process_result = await execute_process_or_raise(
        **implicitly(
            JvmProcess(
                jdk=jdk,
                classpath_entries=[f"{jdk.java_home}/lib/tools.jar"],
                argv=[
                    "com.sun.tools.javac.Main",
                    "-cp",
                    ":".join(materialized_classpath.classpath_entries()),
                    "-d",
                    dest_dir,
                    _LAUNCHER_BASENAME,
                ],
                input_digest=merged_digest,
                output_directories=(dest_dir,),
                description=f"Compile {_LAUNCHER_BASENAME} import processors with javac",
                level=LogLevel.DEBUG,
                # NB: We do not use nailgun for this process, since it is launched exactly once.
                use_nailgun=False,
            )
        )
    )
    stripped_classfiles_digest = await remove_prefix(
        RemovePrefix(process_result.output_digest, dest_dir)
    )
    return JavaParserCompiledClassfiles(digest=stripped_classfiles_digest)


def rules():
    return (
        *collect_rules(),
        UnionRule(ExportableTool, JavaParser),
    )
