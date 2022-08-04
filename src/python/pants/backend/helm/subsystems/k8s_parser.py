# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import logging
import pkgutil
from dataclasses import dataclass
from pathlib import PurePath

from pants.backend.helm.utils.yaml import YamlPath
from pants.backend.python.goals import lockfile
from pants.backend.python.goals.lockfile import GeneratePythonLockfile
from pants.backend.python.subsystems.python_tool_base import PythonToolRequirementsBase
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import EntryPoint
from pants.backend.python.util_rules import pex
from pants.backend.python.util_rules.pex import PexRequest, VenvPex, VenvPexProcess
from pants.core.goals.generate_lockfiles import GenerateToolLockfileSentinel
from pants.engine.fs import CreateDigest, Digest, FileContent
from pants.engine.process import ProcessResult
from pants.engine.rules import Get, collect_rules, rule
from pants.engine.unions import UnionRule
from pants.util.docutil import git_url
from pants.util.strutil import softwrap

logger = logging.getLogger(__name__)

_HELM_K8S_PARSER_SOURCE = "k8s_parser_main.py"
_HELM_K8S_PARSER_PACKAGE = "pants.backend.helm.subsystems"


class HelmKubeParserSubsystem(PythonToolRequirementsBase):
    options_scope = "helm-k8s-parser"
    help = "Used perform modifications to the final output produced by Helm charts when they've been fully rendered."

    default_version = "hikaru==0.11.0b"
    # default_extra_requirements = ["kubernetes-stubs==22.6.0.post1"]

    register_interpreter_constraints = True
    default_interpreter_constraints = ["CPython>=3.7,<3.10"]

    register_lockfile = True
    default_lockfile_resource = (_HELM_K8S_PARSER_PACKAGE, "k8s_parser.lock")
    default_lockfile_path = (
        f"src/python/{_HELM_K8S_PARSER_PACKAGE.replace('.', '/')}/k8s_parser.lock"
    )
    default_lockfile_url = git_url(default_lockfile_path)


class HelmKubeParserLockfileSentinel(GenerateToolLockfileSentinel):
    resolve_name = HelmKubeParserSubsystem.options_scope


@rule
def setup_k8s_parser_lockfile_request(
    _: HelmKubeParserLockfileSentinel,
    post_renderer: HelmKubeParserSubsystem,
    python_setup: PythonSetup,
) -> GeneratePythonLockfile:
    return GeneratePythonLockfile.from_tool(
        post_renderer, use_pex=python_setup.generate_lockfiles_with_pex
    )


@dataclass(frozen=True)
class _HelmKubeParserTool:
    pex: VenvPex


@rule
async def build_k8s_parser_tool(k8s_parser: HelmKubeParserSubsystem) -> _HelmKubeParserTool:
    parser_sources = pkgutil.get_data(_HELM_K8S_PARSER_PACKAGE, _HELM_K8S_PARSER_SOURCE)
    if not parser_sources:
        raise ValueError(
            f"Unable to find source to {_HELM_K8S_PARSER_SOURCE!r} in {_HELM_K8S_PARSER_PACKAGE}"
        )

    parser_file_content = FileContent(
        path="__k8s_parser.py", content=parser_sources, is_executable=True
    )
    parser_digest = await Get(Digest, CreateDigest([parser_file_content]))

    parser_pex = await Get(
        VenvPex,
        PexRequest,
        k8s_parser.to_pex_request(
            main=EntryPoint(PurePath(parser_file_content.path).stem), sources=parser_digest
        ),
    )
    return _HelmKubeParserTool(parser_pex)


@dataclass(frozen=True)
class ParsedKubeManifest:
    filename: str
    found_image_refs: tuple[tuple[int, YamlPath, str], ...]


@dataclass(frozen=True)
class ParseKubeManifestRequest:
    filename: str
    digest: Digest


@rule
async def parse_kube_manifest(
    request: ParseKubeManifestRequest, tool: _HelmKubeParserTool
) -> ParsedKubeManifest:
    result = await Get(
        ProcessResult,
        VenvPexProcess(
            tool.pex,
            argv=[request.filename],
            input_digest=request.digest,
            description="Parsing test",
        ),
    )

    output = result.stdout.decode("utf-8").splitlines()
    image_refs: list[tuple[int, YamlPath, str]] = []
    for line in output:
        parts = line.split(",")
        if len(parts) != 3:
            raise Exception(
                softwrap(
                    f"""Unexpected output from k8s parser when parsing file {request.filename}:

                {line}
                """
                )
            )

        image_refs.append((int(parts[0]), YamlPath.parse(parts[1]), parts[2]))

    return ParsedKubeManifest(filename=request.filename, found_image_refs=tuple(image_refs))


def rules():
    return [
        *collect_rules(),
        *pex.rules(),
        *lockfile.rules(),
        UnionRule(GenerateToolLockfileSentinel, HelmKubeParserLockfileSentinel),
    ]