# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from dataclasses import dataclass

from pants.backend.haskell.util_rules import tools
from pants.backend.haskell.util_rules.tools import GhcBinary
from pants.engine.fs import Digest
from pants.engine.process import Process, ProcessResult
from pants.engine.rules import Get, collect_rules, rule


@dataclass(frozen=True)
class CompileHaskellRequest:
    filename: str
    digest: Digest
    description: str


@dataclass(frozen=True)
class CompiledHaskellResult:
    output: Digest


@rule
async def compile_haskell(request: CompileHaskellRequest, ghc: GhcBinary) -> CompiledHaskellResult:
    process_result = await Get(
        ProcessResult,
        Process(
            [ghc.binary_path.path, request.filename],
            input_digest=request.digest,
            description=request.description,
            env=ghc.env,
            output_files=[request.filename],
        ),
    )
    return CompiledHaskellResult(process_result.output_digest)


def rules():
    return [*collect_rules(), *tools.rules()]
