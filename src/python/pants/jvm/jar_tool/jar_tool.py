# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import importlib.resources
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, unique

from pants.base.glob_match_error_behavior import GlobMatchErrorBehavior
from pants.core.goals.resolves import ExportableTool
from pants.engine.fs import (
    CreateDigest,
    Digest,
    DigestSubset,
    Directory,
    FileContent,
    FileEntry,
    MergeDigests,
    PathGlobs,
    RemovePrefix,
)
from pants.engine.intrinsics import (
    create_digest,
    digest_subset_to_digest,
    get_digest_entries,
    merge_digests,
    remove_prefix,
)
from pants.engine.process import execute_process_or_raise
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.unions import UnionRule
from pants.jvm.jdk_rules import InternalJdk, JvmProcess
from pants.jvm.jdk_rules import rules as jdk_rules
from pants.jvm.resolve.coursier_fetch import ToolClasspathRequest, materialize_classpath_for_tool
from pants.jvm.resolve.coursier_fetch import rules as coursier_fetch_rules
from pants.jvm.resolve.jvm_tool import GenerateJvmLockfileFromTool, JvmToolBase
from pants.jvm.resolve.jvm_tool import rules as jvm_tool_rules
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel


class JarTool(JvmToolBase):
    options_scope = "jar_tool"
    help = "The Java Archive Tool"

    default_artifacts = (
        "args4j:args4j:2.33",
        "com.google.code.findbugs:jsr305:3.0.2",
        "com.google.guava:guava:18.0",
    )
    default_lockfile_resource = (
        "pants.jvm.jar_tool",
        "jar_tool.lock",
    )


@unique
class JarDuplicateAction(Enum):
    SKIP = "skip"
    REPLACE = "replace"
    CONCAT = "concat"
    CONCAT_TEXT = "concat_text"
    THROW = "throw"


@dataclass(frozen=True)
class JarToolRequest:
    jar_name: str
    digest: Digest
    main_class: str | None
    classpath_entries: tuple[str, ...]
    manifest: str | None
    jars: tuple[str, ...]
    file_mappings: FrozenDict[str, str]
    default_action: JarDuplicateAction | None
    policies: tuple[tuple[str, JarDuplicateAction], ...]
    skip: tuple[str, ...]
    compress: bool
    update: bool

    def __init__(
        self,
        *,
        jar_name: str,
        digest: Digest,
        main_class: str | None = None,
        classpath_entries: Iterable[str] | None = None,
        manifest: str | None = None,
        jars: Iterable[str] | None = None,
        file_mappings: Mapping[str, str] | None = None,
        files: Iterable[str] | None = None,
        default_action: JarDuplicateAction | None = None,
        policies: Iterable[tuple[str, str | JarDuplicateAction]] | None = None,
        skip: Iterable[str] | None = None,
        compress: bool = False,
        update: bool = False,
    ) -> None:
        _file_mappings = {**(file_mappings or {}), **({f: f for f in (files or [])})}

        object.__setattr__(self, "jar_name", jar_name)
        object.__setattr__(self, "digest", digest)
        object.__setattr__(self, "main_class", main_class)
        object.__setattr__(self, "manifest", manifest)
        object.__setattr__(self, "classpath_entries", tuple(classpath_entries or ()))
        object.__setattr__(self, "jars", tuple(jars or ()))
        object.__setattr__(self, "file_mappings", FrozenDict(_file_mappings))
        object.__setattr__(self, "default_action", default_action)
        object.__setattr__(self, "policies", tuple(JarToolRequest.__parse_policies(policies or ())))
        object.__setattr__(self, "skip", tuple(skip or ()))
        object.__setattr__(self, "compress", compress)
        object.__setattr__(self, "update", update)

    @staticmethod
    def __parse_policies(
        policies: Iterable[tuple[str, str | JarDuplicateAction]],
    ) -> Iterable[tuple[str, JarDuplicateAction]]:
        return [
            (
                pattern,
                (
                    action
                    if isinstance(action, JarDuplicateAction)
                    else JarDuplicateAction(action.lower())
                ),
            )
            for (pattern, action) in policies
        ]


_JAR_TOOL_MAIN_CLASS = "org.pantsbuild.tools.jar.Main"


@dataclass(frozen=True)
class JarToolCompiledClassfiles:
    digest: Digest


@rule
async def run_jar_tool(
    request: JarToolRequest, jdk: InternalJdk, tool: JarTool, jar_tool: JarToolCompiledClassfiles
) -> Digest:
    output_prefix = "__out"
    output_jarname = os.path.join(output_prefix, request.jar_name)

    tool_classpath, empty_output_digest = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(lockfile=GenerateJvmLockfileFromTool.create(tool))
        ),
        create_digest(CreateDigest([Directory(output_prefix)])),
    )

    toolcp_prefix = "__toolcp"
    jartoolcp_prefix = "__jartoolcp"
    input_prefix = "__in"
    immutable_input_digests = {
        toolcp_prefix: tool_classpath.digest,
        jartoolcp_prefix: jar_tool.digest,
        input_prefix: request.digest,
    }

    policies = ",".join(
        f"{pattern}={action.value.upper()}" for (pattern, action) in request.policies
    )
    file_mappings = ",".join(
        f"{os.path.join(input_prefix, fs_path)}={jar_path}"
        for fs_path, jar_path in request.file_mappings.items()
    )

    tool_process = JvmProcess(
        jdk=jdk,
        argv=[
            _JAR_TOOL_MAIN_CLASS,
            output_jarname,
            *((f"-main={request.main_class}",) if request.main_class else ()),
            *(
                (f"-classpath={','.join(request.classpath_entries)}",)
                if request.classpath_entries
                else ()
            ),
            *(
                (f"-manifest={os.path.join(input_prefix, request.manifest)}",)
                if request.manifest
                else ()
            ),
            *(
                (f"-jars={','.join([os.path.join(input_prefix, jar) for jar in request.jars])}",)
                if request.jars
                else ()
            ),
            *((f"-files={file_mappings}",) if file_mappings else ()),
            *(
                (f"-default_action={request.default_action.value.upper()}",)
                if request.default_action
                else ()
            ),
            *((f"-policies={policies}",) if policies else ()),
            *((f"-skip={','.join(request.skip)}",) if request.skip else ()),
            *(("-compress",) if request.compress else ()),
            *(("-update",) if request.update else ()),
        ],
        classpath_entries=[*tool_classpath.classpath_entries(toolcp_prefix), jartoolcp_prefix],
        input_digest=empty_output_digest,
        extra_immutable_input_digests=immutable_input_digests,
        extra_nailgun_keys=immutable_input_digests.keys(),
        description=f"Building jar {request.jar_name}",
        output_directories=(output_prefix,),
        level=LogLevel.DEBUG,
    )

    result = await execute_process_or_raise(**implicitly({tool_process: JvmProcess}))
    return await remove_prefix(RemovePrefix(result.output_digest, output_prefix))


_JAR_TOOL_SRC_PACKAGES = ["args4j", "jar_tool_source"]


def _load_jar_tool_sources() -> list[FileContent]:
    parent_module = ".".join(__name__.split(".")[:-1])
    result = []
    for package in _JAR_TOOL_SRC_PACKAGES:
        # pkg_path = package.replace(".", os.path.sep)
        # relative_folder = os.path.join("src", pkg_path)
        for resource in importlib.resources.files(parent_module).joinpath(package).iterdir():
            if not resource.is_file():
                continue
            result.append(
                FileContent(
                    path=os.path.join(package, resource.name),
                    content=resource.read_bytes(),
                )
            )
    return result


# TODO(13879): Consolidate compilation of wrapper binaries to common rules.
@rule
async def build_jar_tool(jdk: InternalJdk, tool: JarTool) -> JarToolCompiledClassfiles:
    source_digest = await create_digest(CreateDigest(_load_jar_tool_sources()))

    dest_dir = "classfiles"
    materialized_classpath, java_subset_digest, empty_dest_dir = await concurrently(
        materialize_classpath_for_tool(
            ToolClasspathRequest(
                prefix="__toolcp", lockfile=GenerateJvmLockfileFromTool.create(tool)
            )
        ),
        digest_subset_to_digest(
            DigestSubset(
                source_digest,
                PathGlobs(
                    ["**/*.java"],
                    glob_match_error_behavior=GlobMatchErrorBehavior.error,
                    description_of_origin="jar tool sources",
                ),
            )
        ),
        create_digest(CreateDigest([Directory(path=dest_dir)])),
    )

    merged_digest, src_entries = await concurrently(
        merge_digests(MergeDigests([materialized_classpath.digest, source_digest, empty_dest_dir])),
        get_digest_entries(java_subset_digest),
    )

    compile_result = await execute_process_or_raise(
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
                    *[entry.path for entry in src_entries if isinstance(entry, FileEntry)],
                ],
                input_digest=merged_digest,
                output_directories=(dest_dir,),
                description="Compile jar-tool sources using javac.",
                level=LogLevel.DEBUG,
                use_nailgun=False,
            )
        )
    )

    stripped_classfiles_digest = await remove_prefix(
        RemovePrefix(compile_result.output_digest, dest_dir)
    )
    return JarToolCompiledClassfiles(digest=stripped_classfiles_digest)


def rules():
    return [
        *collect_rules(),
        *coursier_fetch_rules(),
        *jdk_rules(),
        *jvm_tool_rules(),
        UnionRule(ExportableTool, JarTool),
    ]
