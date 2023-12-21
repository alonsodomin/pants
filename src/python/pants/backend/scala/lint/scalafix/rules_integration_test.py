# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from textwrap import dedent

import pytest

from internal_plugins.test_lockfile_fixtures.lockfile_fixture import (
    JVMLockfileFixture,
    JVMLockfileFixtureDefinition,
)
from pants.backend.scala import target_types
from pants.backend.scala.compile import scalac
from pants.backend.scala.compile.semanticdb.rules import rules as semanticdb_rules
from pants.backend.scala.lint.scalafix import skip_field
from pants.backend.scala.lint.scalafix.rules import (
    GatherScalafixConfigFilesRequest,
    PartitionInfo,
    ScalafixConfigFiles,
    ScalafixFieldSet,
    ScalafixRequest,
)
from pants.backend.scala.lint.scalafix.rules import rules as scalafix_rules
from pants.backend.scala.lint.scalafix.subsystem import DEFAULT_SCALAFIX_CONFIG_FILENAME
from pants.backend.scala.resolve.artifact import rules as scala_artifact_rules
from pants.backend.scala.target_types import (
    ScalacPluginTarget,
    ScalaSourcesGeneratorTarget,
    ScalaSourceTarget,
)
from pants.build_graph.address import Address
from pants.core.goals.fix import FixResult
from pants.core.goals.fmt import FmtResult, Partitions
from pants.core.util_rules import config_files, source_files, stripped_source_files
from pants.core.util_rules.external_tool import rules as external_tool_rules
from pants.engine.fs import PathGlobs, Snapshot
from pants.engine.rules import QueryRule
from pants.engine.target import Target
from pants.jvm import classpath
from pants.jvm.dependency_inference import artifact_mapper
from pants.jvm.jdk_rules import rules as jdk_rules
from pants.jvm.resolve.coursier_fetch import rules as coursier_fetch_rules
from pants.jvm.resolve.coursier_setup import rules as coursier_setup_rules
from pants.jvm.strip_jar import strip_jar
from pants.jvm.target_types import JvmArtifactTarget
from pants.jvm.testutil import maybe_skip_jdk_test
from pants.jvm.util_rules import rules as util_rules
from pants.testutil.rule_runner import PYTHON_BOOTSTRAP_ENV, RuleRunner, logging
from pants.util.logging import LogLevel


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
            *stripped_source_files.rules(),
            *artifact_mapper.rules(),
            *strip_jar.rules(),
            *scalac.rules(),
            *semanticdb_rules(),
            *util_rules(),
            *jdk_rules(),
            *target_types.rules(),
            *scala_artifact_rules(),
            *scalafix_rules(),
            *skip_field.rules(),
            QueryRule(Partitions, (ScalafixRequest.PartitionRequest,)),
            QueryRule(FmtResult, (ScalafixRequest.Batch,)),
            QueryRule(Snapshot, (PathGlobs,)),
            QueryRule(ScalafixConfigFiles, (GatherScalafixConfigFilesRequest,)),
        ],
        target_types=[
            ScalaSourceTarget,
            ScalacPluginTarget,
            ScalaSourcesGeneratorTarget,
            JvmArtifactTarget,
        ],
    )
    return rule_runner


def test_gather_scalafix_config_files(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            DEFAULT_SCALAFIX_CONFIG_FILENAME: "",
            f"foo/bar/{DEFAULT_SCALAFIX_CONFIG_FILENAME}": "",
            f"hello/{DEFAULT_SCALAFIX_CONFIG_FILENAME}": "",
            "hello/Foo.scala": "",
            "hello/world/Foo.scala": "",
            "foo/bar/Foo.scala": "",
            "foo/bar/xyyzzy/Foo.scala": "",
            "foo/blah/Foo.scala": "",
        }
    )

    snapshot = rule_runner.request(Snapshot, [PathGlobs(["**/*.scala"])])
    request = rule_runner.request(
        ScalafixConfigFiles, [GatherScalafixConfigFilesRequest(snapshot.files)]
    )
    assert sorted(request.source_dir_to_config_file.items()) == [
        ("foo/bar", "foo/bar/.scalafix.conf"),
        ("foo/bar/xyyzzy", "foo/bar/.scalafix.conf"),
        ("foo/blah", ".scalafix.conf"),
        ("hello", "hello/.scalafix.conf"),
        ("hello/world", "hello/.scalafix.conf"),
    ]


def run_scalafix(
    rule_runner: RuleRunner,
    targets: list[Target],
    extra_options: list[str] = [],
    expected_partitions: dict[str, tuple[str, ...]] | None = None,
) -> FixResult | list[FixResult]:
    rule_runner.set_options(extra_options, env_inherit=PYTHON_BOOTSTRAP_ENV)

    field_sets = [ScalafixFieldSet.create(tgt) for tgt in targets]
    partitions = rule_runner.request(
        Partitions[PartitionInfo], [ScalafixRequest.PartitionRequest(tuple(field_sets))]
    )

    fix_results = [
        rule_runner.request(
            FixResult,
            [
                ScalafixRequest.Batch(
                    "",
                    partition.elements,
                    partition_metadata=partition.metadata,
                    snapshot=rule_runner.request(Snapshot, [PathGlobs(partition.elements)]),
                )
            ],
        )
        for partition in partitions
    ]
    return fix_results if expected_partitions else fix_results[0]


BAD_FILE = """\
object Foo {
  def throwException = throw new IllegalArgumentException
}
"""


@maybe_skip_jdk_test
def test_failure(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "Foo.scala": BAD_FILE,
            "BUILD": "scala_sources(name='test')",
            ".scalafix.conf": "rules = [ DisableSyntax ]",
        }
    )

    tgt = rule_runner.get_target(Address("", target_name="test", relative_file_path="Foo.scala"))

    fix_result = run_scalafix(
        rule_runner,
        [tgt],
        extra_options=["--scalac-semanticdb-enabled=False"],
    )
    assert isinstance(fix_result, FixResult)
    assert fix_result.output == rule_runner.make_snapshot({"Foo.scala": BAD_FILE})
    assert not fix_result.did_change


@pytest.fixture
def semanticdb_lockfile_def() -> JVMLockfileFixtureDefinition:
    return JVMLockfileFixtureDefinition(
        "semanticdb-scalac-2.13.test.lock",
        ["org.scala-lang:scala-library:2.13.12", "org.scalameta:semanticdb-scalac_2.13.12:4.8.14"],
    )


@pytest.fixture
def semanticdb_lockfile(
    semanticdb_lockfile_def: JVMLockfileFixtureDefinition, request
) -> JVMLockfileFixture:
    return semanticdb_lockfile_def.load(request)


@logging(level=LogLevel.INFO)
@maybe_skip_jdk_test
def test_remove_unused(rule_runner: RuleRunner, semanticdb_lockfile: JVMLockfileFixture) -> None:
    rule_runner.write_files(
        {
            "3rdparty/jvm/default.lock": semanticdb_lockfile.serialized_lockfile,
            "3rdparty/jvm/BUILD": semanticdb_lockfile.requirements_as_jvm_artifact_targets(),
            "src/jvm/Foo.scala": dedent(
                """\
                import scala.List
                import scala.collection.{immutable, mutable}
                object Foo { immutable.Seq.empty[Int] }
                """
            ),
            "src/jvm/BUILD": "scala_sources()",
            ".scalafix.conf": "rules = [ RemoveUnused ]",
        }
    )

    tgt = rule_runner.get_target(Address("src/jvm", relative_file_path="Foo.scala"))

    scalac_args = ["-Xlint:unused"]
    fix_result = run_scalafix(
        rule_runner,
        [tgt],
        extra_options=[
            f"--source-root-patterns={repr(['src/jvm'])}",
            f"--scalac-args={repr(scalac_args)}",
        ],
    )
    assert isinstance(fix_result, FixResult)
    assert fix_result.output == rule_runner.make_snapshot(
        {
            "src/jvm/Foo.scala": dedent(
                """
                import scala.collection.immutable
                object Foo { immutable.Seq.empty[Int] }
                """
            )
        }
    )
    assert fix_result.did_change is True


@pytest.fixture
def scala_rewrites_lockfile_def() -> JVMLockfileFixtureDefinition:
    return JVMLockfileFixtureDefinition(
        "scala-rewrites-2.13.test.lock",
        [
            "org.scala-lang:scala-library:2.13.12",
            "org.scalameta:semanticdb-scalac_2.13.12:4.8.14",
            "org.scala-lang:scala-rewrites_2.13:0.1.5",
        ],
    )


@pytest.fixture
def scala_rewrites_lockfile(
    scala_rewrites_lockfile_def: JVMLockfileFixtureDefinition, request
) -> JVMLockfileFixture:
    return scala_rewrites_lockfile_def.load(request)


@logging
@maybe_skip_jdk_test
def test_run_custom_rule(
    rule_runner: RuleRunner, scala_rewrites_lockfile: JVMLockfileFixture
) -> None:
    rule_runner.write_files(
        {
            "3rdparty/jvm/default.lock": scala_rewrites_lockfile.serialized_lockfile,
            "3rdparty/jvm/BUILD": scala_rewrites_lockfile.requirements_as_jvm_artifact_targets(),
            "src/jvm/Foo.scala": dedent(
                """\
                object Foo {
                    def hello = "hello"
                    def nil = Nil + hello
                }
                """
            ),
            "src/jvm/BUILD": "scala_sources(name='test')",
            ".scalafix.conf": "rules = [ fix.scala213.Any2StringAdd ]",
        }
    )

    tgt = rule_runner.get_target(
        Address("src/jvm", target_name="test", relative_file_path="Foo.scala")
    )

    extra_rule_targets = ["3rdparty/jvm:org.scala-lang_scala-rewrites_2.13"]
    fix_result = run_scalafix(
        rule_runner,
        [tgt],
        extra_options=[
            f"--source-root-patterns={repr(['src/jvm'])}",
            f"--scalac-semanticdb-extra-options={repr({'synthetics': 'on'})}",
            f"--scalafix-extra-rule-targets={repr(extra_rule_targets)}",
        ],
    )
    assert isinstance(fix_result, FixResult)
    assert fix_result.output == rule_runner.make_snapshot(
        {
            "src/jvm/Foo.scala": dedent(
                """\
                object Foo {
                    def hello = "hello"
                    def nil = String.valueOf(Nil) + hello
                }
                """
            )
        }
    )
    assert fix_result.did_change is True
