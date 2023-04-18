# Copyright 2021 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from dataclasses import dataclass
from typing import Tuple

import pkg_resources

from pants.core.goals.generate_lockfiles import DEFAULT_TOOL_LOCKFILE, GenerateToolLockfileSentinel
from pants.engine.fs import AddPrefix, CreateDigest, Digest, FileContent
from pants.engine.internals.native_engine import RemovePrefix
from pants.engine.process import FallibleProcessResult, ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.unions import UnionRule
from pants.jvm import wrapped_binaries
from pants.jvm.compile import ClasspathEntry
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, GenerateJvmToolLockfileSentinel
from pants.jvm.wrapped_binaries import CompileJvmWrappedBinaryRequest
from pants.util.logging import LogLevel
from pants.util.ordered_set import FrozenOrderedSet

_STRIP_JAR_BASENAME = "StripJar.java"
_OUTPUT_PATH = "__stripped_jars"


class StripJarToolLockfileSentinel(GenerateJvmToolLockfileSentinel):
    resolve_name = "strip-jar"


@dataclass(frozen=True)
class StripJarRequest:
    digest: Digest
    filenames: Tuple[str, ...]


@dataclass(frozen=True)
class FallibleStripJarResult:
    process_result: FallibleProcessResult


@dataclass(frozen=True)
class StripJarBinary:
    classpath: ClasspathEntry


@rule(level=LogLevel.DEBUG)
async def strip_jar(
    processor: StripJarBinary,
    jdk: InternalJdk,
    request: StripJarRequest,
) -> Digest:
    filenames = list(request.filenames)

    if len(filenames) == 0:
        return request.digest

    input_path = "__jars_to_strip"
    toolcp_relpath = "__toolcp"
    processorcp_relpath = "__processorcp"

    lockfile_request = await Get(GenerateJvmLockfileFromTool, StripJarToolLockfileSentinel())

    tool_classpath, prefixed_jars_digest = await MultiGet(
        Get(
            ToolClasspath,
            ToolClasspathRequest(lockfile=lockfile_request),
        ),
        Get(Digest, AddPrefix(request.digest, input_path)),
    )

    extra_immutable_input_digests = {
        toolcp_relpath: tool_classpath.digest,
        processorcp_relpath: processor.classpath.digest,
    }

    process_result = await Get(
        ProcessResult,
        JvmProcess(
            jdk=jdk,
            classpath_entries=[
                *tool_classpath.classpath_entries(toolcp_relpath),
                processorcp_relpath,
            ],
            argv=["org.pantsbuild.stripjar.StripJar", input_path, _OUTPUT_PATH, *filenames],
            input_digest=prefixed_jars_digest,
            extra_immutable_input_digests=extra_immutable_input_digests,
            output_directories=(_OUTPUT_PATH,),
            extra_nailgun_keys=extra_immutable_input_digests,
            description=f"Stripping jar {filenames[0]}",
            level=LogLevel.DEBUG,
        ),
    )

    return await Get(Digest, RemovePrefix(process_result.output_digest, _OUTPUT_PATH))


def _load_strip_jar_source() -> bytes:
    return pkg_resources.resource_string(__name__, _STRIP_JAR_BASENAME)


@rule
async def build_strip_jar_processor() -> StripJarBinary:
    lockfile_request, source_digest = await MultiGet(
        Get(GenerateJvmLockfileFromTool, StripJarToolLockfileSentinel()),
        Get(
            Digest,
            CreateDigest(
                [
                    FileContent(
                        path=_STRIP_JAR_BASENAME,
                        content=_load_strip_jar_source(),
                    ),
                ]
            ),
        ),
    )

    classpath_entry = await Get(
        ClasspathEntry,
        CompileJvmWrappedBinaryRequest,
        CompileJvmWrappedBinaryRequest.for_java_sources(
            name="strip_jar", sources=source_digest, lockfile_request=lockfile_request
        ),
    )
    return StripJarBinary(classpath_entry)


@rule
def generate_strip_jar_lockfile_request(
    _: StripJarToolLockfileSentinel,
) -> GenerateJvmLockfileFromTool:
    return GenerateJvmLockfileFromTool(
        artifact_inputs=FrozenOrderedSet(
            {
                "io.github.zlika:reproducible-build-maven-plugin:0.16",
            }
        ),
        artifact_option_name="n/a",
        lockfile_option_name="n/a",
        resolve_name=StripJarToolLockfileSentinel.resolve_name,
        read_lockfile_dest=DEFAULT_TOOL_LOCKFILE,
        write_lockfile_dest="src/python/pants/jvm/strip_jar/strip_jar.lock",
        default_lockfile_resource=(
            "pants.jvm.strip_jar",
            "strip_jar.lock",
        ),
    )


def rules():
    return [
        *collect_rules(),
        *wrapped_binaries.rules(),
        UnionRule(GenerateToolLockfileSentinel, StripJarToolLockfileSentinel),
    ]
