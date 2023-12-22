# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from pants.backend.java.subsystems.jmh import Jmh
from pants.core.goals.bench import (
    BenchmarkExtraEnv,
    BenchmarkFieldSet,
    BenchmarkRequest,
    BenchmarkResult,
    BenchmarkSubsystem,
)
from pants.core.goals.generate_lockfiles import GenerateToolLockfileSentinel
from pants.core.target_types import FileSourceField
from pants.core.util_rules.source_files import SourceFiles, SourceFilesRequest
from pants.core.util_rules.system_binaries import UnzipBinary
from pants.engine.addresses import Address, Addresses
from pants.engine.engine_aware import EngineAwareParameter
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest
from pants.engine.fs import (
    CreateDigest,
    Digest,
    DigestSubset,
    Directory,
    FileContent,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
    Snapshot,
)
from pants.engine.process import FallibleProcessResult, Process, ProcessCacheScope, ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.engine.target import SourcesField, TransitiveTargets, TransitiveTargetsRequest
from pants.engine.unions import UnionRule
from pants.jvm.classpath import Classpath
from pants.jvm.compile import ClasspathEntry
from pants.jvm.goals import lockfile
from pants.jvm.jdk_rules import JdkEnvironment, JdkRequest, JvmProcess
from pants.jvm.resolve.coursier_fetch import ToolClasspath, ToolClasspathRequest
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, GenerateJvmToolLockfileSentinel
from pants.jvm.target_types import (
    JmhBenchmarkExtraEnvVarsField,
    JmhBenchmarkSourceField,
    JmhBenchmarkTimeoutField,
    JvmDependenciesField,
    JvmJdkField,
    JvmResolveField,
)
from pants.util.logging import LogLevel


@dataclass(frozen=True)
class JmhBenchmarkFieldSet(BenchmarkFieldSet):
    required_fields = (JmhBenchmarkSourceField, JvmJdkField)

    sources: JmhBenchmarkSourceField
    timeout: JmhBenchmarkTimeoutField
    jdk_version: JvmJdkField
    resolve: JvmResolveField
    dependencies: JvmDependenciesField
    extra_env_vars: JmhBenchmarkExtraEnvVarsField


class JmhBenchmarkRequest(BenchmarkRequest):
    tool_subsystem = Jmh
    field_set_type = JmhBenchmarkFieldSet


class JmhToolLockfileSentinel(GenerateJvmToolLockfileSentinel):
    resolve_name = Jmh.options_scope


@dataclass(frozen=True)
class JmhSetupRequest:
    field_set: JmhBenchmarkFieldSet


@dataclass(frozen=True)
class JmhSetup:
    process: JvmProcess
    reports_dir_prefix: str


@dataclass(frozen=True)
class GeneratedJmhSources:
    sources: Snapshot
    resources: Snapshot


@dataclass(frozen=True)
class GenerateJmhSourcesRequest(EngineAwareParameter):
    address: Address
    jdk: JdkEnvironment
    jmh_classpath: ToolClasspath
    classpath: Classpath

    def debug_hint(self) -> str | None:
        return self.address.spec


async def _compile_jmh_generated_sources(
    address: Address,
    jdk: JdkEnvironment,
    jmh_classpath: ToolClasspath,
    classpath: Classpath,
    sources: Snapshot,
) -> Digest:
    dest_dir = "__gen_classes"

    empty_dest_dir = await Get(Digest, CreateDigest([Directory(dest_dir)]))
    classpath_digest, merged_digest = await MultiGet(
        Get(Digest, MergeDigests(classpath.digests())),
        Get(Digest, MergeDigests([sources.digest, empty_dest_dir])),
    )

    jmh_prefix = "__jmh"
    classes_prefix = "__classes"
    immutable_input_digests = {jmh_prefix: jmh_classpath.digest, classes_prefix: classpath_digest}

    process_result = await Get(
        ProcessResult,
        JvmProcess(
            jdk=jdk,
            classpath_entries=[f"{jdk.java_home}/lib/tools.jar"],
            argv=[
                "com.sun.tools.javac.Main",
                "-cp",
                ":".join(
                    [
                        *jmh_classpath.classpath_entries(jmh_prefix),
                        *classpath.args(prefix=classes_prefix),
                    ]
                ),
                "-d",
                dest_dir,
                *sources.files,
            ],
            extra_immutable_input_digests=immutable_input_digests,
            input_digest=merged_digest,
            output_directories=(dest_dir,),
            description=f"Compile generated JMH sources for {address} ",
            level=LogLevel.DEBUG,
            # NB: We do not use nailgun for this process, since it is launched exactly once.
            use_nailgun=False,
        ),
    )
    return await Get(Digest, RemovePrefix(process_result.output_digest, dest_dir))


@rule(level=LogLevel.DEBUG)
async def setup_jmh_for_target(
    request: JmhSetupRequest,
    jmh: Jmh,
    bench_subsystem: BenchmarkSubsystem,
    bench_extra_env: BenchmarkExtraEnv,
) -> JmhSetup:
    jdk, transitive_tgts = await MultiGet(
        Get(JdkEnvironment, JdkRequest, JdkRequest.from_field(request.field_set.jdk_version)),
        Get(TransitiveTargets, TransitiveTargetsRequest([request.field_set.address])),
    )

    lockfile_request = await Get(GenerateJvmLockfileFromTool, JmhToolLockfileSentinel())
    classpath, jmh_classpath, files = await MultiGet(
        Get(Classpath, Addresses([request.field_set.address])),
        Get(ToolClasspath, ToolClasspathRequest(lockfile=lockfile_request)),
        Get(
            SourceFiles,
            SourceFilesRequest(
                (dep.get(SourcesField) for dep in transitive_tgts.dependencies),
                for_sources_types=(FileSourceField,),
                enable_codegen=True,
            ),
        ),
    )

    classpath_digest, jmh_generated = await MultiGet(
        Get(Digest, MergeDigests(classpath.digests())),
        Get(
            GeneratedJmhSources,
            GenerateJmhSourcesRequest(request.field_set.address, jdk, jmh_classpath, classpath),
        ),
    )
    jmh_generated_classfiles = await _compile_jmh_generated_sources(
        request.field_set.address, jdk, jmh_classpath, classpath, jmh_generated.sources
    )
    jmh_generated_digest = await Get(
        Digest, MergeDigests([jmh_generated_classfiles, jmh_generated.resources.digest])
    )

    toolcp_relpath = "__toolcp"
    classes_relpath = "__classes"
    gen_relpath = "__gen"
    extra_immutable_input_digests = {
        toolcp_relpath: jmh_classpath.digest,
        classes_relpath: classpath_digest,
        gen_relpath: jmh_generated_digest,
    }

    reports_dir_prefix = "__reports"
    report_file = os.path.join(reports_dir_prefix, request.field_set.address.path_safe_spec)
    report_file_digest = await Get(
        Digest, CreateDigest([FileContent(path=report_file, content=b"")])
    )

    input_digest = await Get(Digest, MergeDigests([files.snapshot.digest, report_file_digest]))

    # Cache bench runs only if they are successful, or not at all if `--bench-force`.
    cache_scope = (
        ProcessCacheScope.PER_SESSION if bench_subsystem.force else ProcessCacheScope.SUCCESSFUL
    )

    field_set_extra_env = await Get(
        EnvironmentVars, EnvironmentVarsRequest(request.field_set.extra_env_vars.value or ())
    )

    process = JvmProcess(
        jdk=jdk,
        classpath_entries=[
            *classpath.args(prefix=classes_relpath),
            *jmh_classpath.classpath_entries(toolcp_relpath),
            gen_relpath,
        ],
        argv=[
            "org.openjdk.jmh.Main",
            *(
                ("-foe", ("true" if jmh.fail_on_error else "false"))
                if jmh.fail_on_error is not None
                else ()
            ),
            "-v",
            jmh.verbosity.value,
            "-rf",
            jmh.result_format.value,
            "-rff",
            report_file,
            *jmh.args,
        ],
        input_digest=input_digest,
        extra_env={**bench_extra_env.env, **field_set_extra_env},
        extra_jvm_options=jmh.jvm_options,
        extra_immutable_input_digests=extra_immutable_input_digests,
        output_files=(report_file,),
        description=f"Run JMH benchmarks for {request.field_set.address}",
        timeout_seconds=request.field_set.timeout.calculate_from_global_options(bench_subsystem),
        level=LogLevel.DEBUG,
        cache_scope=cache_scope,
        use_nailgun=False,
    )
    return JmhSetup(process=process, reports_dir_prefix=reports_dir_prefix)


@rule(desc="Run JMH", level=LogLevel.DEBUG)
async def run_jmh_benchmark(
    bench_subsystem: BenchmarkSubsystem,
    batch: JmhBenchmarkRequest.Batch[JmhBenchmarkFieldSet, Any],
) -> BenchmarkResult:
    field_set = batch.single_element

    jmh_setup = await Get(JmhSetup, JmhSetupRequest(field_set))
    process_result = await Get(FallibleProcessResult, JvmProcess, jmh_setup.process)
    reports_dir_prefix = jmh_setup.reports_dir_prefix

    reports_subset = await Get(
        Digest,
        DigestSubset(
            process_result.output_digest, PathGlobs([os.path.join(reports_dir_prefix, "**")])
        ),
    )
    reports_snapshot = await Get(Snapshot, RemovePrefix(reports_subset, reports_dir_prefix))

    return BenchmarkResult.from_fallible_process_result(
        process_result,
        address=field_set.address,
        output_setting=bench_subsystem.output,
        reports=reports_snapshot,
    )


@dataclass(frozen=True)
class _ExtractClassfiles:
    address: Address
    path: str
    digest: Digest


@rule(level=LogLevel.DEBUG)
async def _extract_classes_from_digest(request: _ExtractClassfiles, unzip: UnzipBinary) -> Digest:
    dest_dir = "__extracted"
    dest_digest = await Get(Digest, CreateDigest([Directory(dest_dir)]))

    input_digest = await Get(Digest, MergeDigests([request.digest, dest_digest]))

    process_result = await Get(
        ProcessResult,
        Process(
            unzip.extract_archive_argv(request.path, dest_dir),
            input_digest=input_digest,
            level=LogLevel.DEBUG,
            description=f"Collecting classfiles for {request.address} from {request.path}.",
            output_directories=(dest_dir,),
        ),
    )

    return await Get(Digest, RemovePrefix(process_result.output_digest, dest_dir))


@rule(level=LogLevel.DEBUG)
async def generate_jmh_sources(request: GenerateJmhSourcesRequest, jmh: Jmh) -> GeneratedJmhSources:
    classfiles_filenames = ClasspathEntry.args(request.classpath.entries)
    classfiles_digests = [entry.digest for entry in request.classpath.entries]
    extracted_classes = await MultiGet(
        Get(Digest, _ExtractClassfiles(request.address, path, digest))
        for path, digest in zip(classfiles_filenames, classfiles_digests)
    )

    input_prefix = "__input"
    input_digest = await Get(Digest, MergeDigests(extracted_classes))

    toolcp_relpath = "__toolcp"
    extra_immutable_input_digests = {
        toolcp_relpath: request.jmh_classpath.digest,
        input_prefix: input_digest,
    }

    sources_output_dir = os.path.join("__gen", "sources")
    files_output_dir = os.path.join("__gen", "files")
    empty_dirs_digest = await Get(
        Digest, CreateDigest([Directory(sources_output_dir), Directory(files_output_dir)])
    )

    process_result = await Get(
        ProcessResult,
        JvmProcess(
            jdk=request.jdk,
            classpath_entries=request.jmh_classpath.classpath_entries(toolcp_relpath),
            argv=[
                "org.openjdk.jmh.generators.bytecode.JmhBytecodeGenerator",
                input_prefix,
                sources_output_dir,
                files_output_dir,
                jmh.generator_type.value,
            ],
            input_digest=empty_dirs_digest,
            extra_jvm_options=jmh.jvm_options,
            extra_immutable_input_digests=extra_immutable_input_digests,
            output_directories=(sources_output_dir, files_output_dir),
            description=f"Generate JMH sources for {request.address}",
            level=LogLevel.DEBUG,
            use_nailgun=False,
        ),
    )

    sources_digest_subset, files_digest_subset = await MultiGet(
        Get(
            Digest,
            DigestSubset(
                process_result.output_digest, PathGlobs([os.path.join(sources_output_dir, "**")])
            ),
        ),
        Get(
            Digest,
            DigestSubset(
                process_result.output_digest, PathGlobs([os.path.join(files_output_dir, "**")])
            ),
        ),
    )

    sources_snapshot, resources_snapshot = await MultiGet(
        Get(Snapshot, RemovePrefix(sources_digest_subset, sources_output_dir)),
        Get(Snapshot, RemovePrefix(files_digest_subset, files_output_dir)),
    )

    return GeneratedJmhSources(sources_snapshot, resources_snapshot)


@rule
def generate_jmh_lockfile_request(
    _: JmhToolLockfileSentinel, jmh: Jmh
) -> GenerateJvmLockfileFromTool:
    return GenerateJvmLockfileFromTool.create(jmh)


def rules():
    return [
        *collect_rules(),
        *lockfile.rules(),
        *JmhBenchmarkRequest.rules(),
        UnionRule(GenerateToolLockfileSentinel, JmhToolLockfileSentinel),
    ]
