# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from textwrap import dedent
from typing import overload
from pants.core.goals.fix import FixResult

import pytest

from pants.backend.scala import target_types
from pants.backend.scala.compile.scalac import rules as scalac_rules
from pants.backend.scala.lint.scalafix import skip_field
from pants.backend.scala.lint.scalafix.rules import (
    GatherScalafixConfigFilesRequest,
    PartitionInfo,
    ScalafixConfigFiles,
    ScalafixFieldSet,
    ScalafixRequest,
)
from pants.backend.scala.lint.scalafix.rules import rules as scalafix_rules
from pants.backend.scala.target_types import ScalaArtifactTarget, ScalaSourcesGeneratorTarget, ScalaSourceTarget, ScalacPluginTarget
from pants.build_graph.address import Address
from pants.core.goals.fmt import FmtResult, Partitions
from pants.core.util_rules import config_files, source_files
from pants.core.util_rules.external_tool import rules as external_tool_rules
from pants.engine.fs import PathGlobs, Snapshot
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.jvm import classpath
from pants.jvm.jdk_rules import rules as jdk_rules
from pants.jvm.resolve.coursier_fetch import rules as coursier_fetch_rules
from pants.jvm.resolve.coursier_setup import rules as coursier_setup_rules
from pants.jvm.strip_jar import strip_jar
from pants.jvm.util_rules import rules as util_rules
from pants.testutil.rule_runner import PYTHON_BOOTSTRAP_ENV, RuleRunner, logging
from pants.util.dirutil import find_nearest_ancestor_file


@pytest.fixture
def rule_runner() -> RuleRunner:
    rule_runner = RuleRunner(
        rules=[
            *config_files.rules(),
            *classpath.rules(),
            *coursier_fetch_rules(),
            *coursier_setup_rules(),
            *external_tool_rules(),
            *source_files.rules(),
            *strip_jar.rules(),
            *scalac_rules(),
            *util_rules(),
            *jdk_rules(),
            *target_types.rules(),
            *scalafix_rules(),
            *skip_field.rules(),
            QueryRule(Partitions, (ScalafixRequest.PartitionRequest,)),
            QueryRule(FmtResult, (ScalafixRequest.Batch,)),
            QueryRule(Snapshot, (PathGlobs,)),
            QueryRule(ScalafixConfigFiles, (GatherScalafixConfigFilesRequest,)),
        ],
        target_types=[ScalaSourceTarget, ScalaArtifactTarget, ScalacPluginTarget, ScalaSourcesGeneratorTarget],
    )
    return rule_runner

def run_scalafix(rule_runner: RuleRunner, targets: list[Target], extra_options: list[str] = [], expected_partitions: dict[str, tuple[str, ...]] | None = None) -> FixResult | list[FixResult]:
    rule_runner.set_options(extra_options, env_inherit=PYTHON_BOOTSTRAP_ENV)
    print(extra_options)
    
    field_sets = [ScalafixFieldSet.create(tgt) for tgt in targets]
    partitions = rule_runner.request(
        Partitions[PartitionInfo],
        [
            ScalafixRequest.PartitionRequest(tuple(field_sets))
        ]
    )

    fix_results = [
        rule_runner.request(
          FixResult,
          [
              ScalafixRequest.Batch(
                "",
                partition.elements,
                partition_metadata=partition.metadata,
                snapshot=rule_runner.request(Snapshot, [PathGlobs(partition.elements)])
              )
          ]
        ) for partition in partitions
    ]
    return fix_results if expected_partitions else fix_results[0]

@logging
def test_remove_unused(rule_runner: RuleRunner) -> None:
    rule_runner.write_files({
        "3rdparty/jvm/BUILD": dedent(
          """\
          scala_artifact(
            name="semanticdb-scalac",
            group="org.scalameta",
            artifact="semanticdb-scalac",
            version="4.8.7",
            crossversion="full"
          )

          scalac_plugin()
          """
        ),
        "Foo.scala": dedent(
          """\
          import scala.List
          import scala.collection.{immutable, mutable}
          object Foo { immutable.Seq.empty[Int] }
          """
        ),
        "BUILD": "scala_sources(name='foo')",
        ".scalafix.conf": "rules = [ RemoveUnused ]"
    })

    tgt = rule_runner.get_target(Address("", target_name="foo", relative_file_path="Foo.scala"))

    scalac_args = ["-Ywarn-unused"]
    fix_result = run_scalafix(rule_runner, [tgt], extra_options=[f"--scalac-args={repr(scalac_args)}"])
    assert fix_result.output == rule_runner.make_snapshot({"Foo.scala": dedent(
        """\
        import scala.collection.immutable
        object Foo { immutable.Seq.empty[Int] }
        """
    )})
    assert fix_result.did_change is True
