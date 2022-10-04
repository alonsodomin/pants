# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import textwrap

import pytest

from pants.backend.haskell.compile import ghc
from pants.backend.haskell.compile.ghc import CompiledHaskellResult, CompileHaskellRequest
from pants.engine import process
from pants.engine.fs import CreateDigest, Digest, FileContent
from pants.engine.process import Process, ProcessResult
from pants.testutil.rule_runner import PYTHON_BOOTSTRAP_ENV, QueryRule, RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    rule_runner = RuleRunner(
        rules=[
            *ghc.rules(),
            *process.rules(),
            QueryRule(CompiledHaskellResult, (CompileHaskellRequest,)),
            QueryRule(ProcessResult, (Process,)),
        ]
    )
    rule_runner.set_options([], env_inherit=PYTHON_BOOTSTRAP_ENV)
    return rule_runner


def test_compile_haskell(rule_runner: RuleRunner) -> None:
    hello_world_digest = rule_runner.request(
        Digest,
        [
            CreateDigest(
                [
                    FileContent(
                        path="HelloWorld.hs",
                        content=textwrap.dedent(
                            """\
                            main :: IO ()
                            main = putStrLn "Hello World"
                            """
                        ).encode("utf-8"),
                    )
                ]
            )
        ],
    )

    compiled_result = rule_runner.request(
        CompiledHaskellResult,
        [
            CompileHaskellRequest(
                filename="HelloWorld", digest=hello_world_digest, description="Compile HelloWorld"
            )
        ],
    )

    hello_world_result = rule_runner.request(
        ProcessResult,
        [
            Process(
                ["HelloWorld"], description="Run Hello World", input_digest=compiled_result.output
            )
        ],
    )
    hello_world_output = hello_world_result.stdout.decode("utf-8").strip()

    assert hello_world_output == "Hello World"
