# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from textwrap import dedent

import pytest

from pants.backend.helm.resolve import fetch
from pants.backend.helm.resolve.artifacts import HelmArtifact, ResolvedHelmArtifact
from pants.backend.helm.resolve.fetch import (
    AllFetchedHelmArtifacts,
    FetchedHelmArtifact,
    FetchHelmArtifactRequest,
)
from pants.backend.helm.target_types import AllHelmArtifactTargets, HelmArtifactTarget
from pants.backend.helm.target_types import rules as target_types_rules
from pants.backend.helm.util_rules import tool
from pants.core.util_rules import config_files, external_tool
from pants.engine import process
from pants.engine.addresses import Address
from pants.engine.rules import QueryRule
from pants.testutil.rule_runner import RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        target_types=[HelmArtifactTarget],
        rules=[
            *config_files.rules(),
            *external_tool.rules(),
            *fetch.rules(),
            *tool.rules(),
            *process.rules(),
            *target_types_rules(),
            QueryRule(AllHelmArtifactTargets, ()),
            QueryRule(ResolvedHelmArtifact, (HelmArtifact,)),
            QueryRule(FetchedHelmArtifact, (FetchHelmArtifactRequest,)),
            QueryRule(AllFetchedHelmArtifacts, ()),
        ],
    )


def test_fetch_single_artifact(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "3rdparty/helm/BUILD": dedent(
                """\
                helm_artifact(
                    name="prometheus-stack",
                    repository="https://prometheus-community.github.io/helm-charts",
                    artifact="kube-prometheus-stack",
                    version="^27.2.0"
                )
                """
            ),
        }
    )

    target = rule_runner.get_target(Address("3rdparty/helm", target_name="prometheus-stack"))

    expected_resolved_artifact = rule_runner.request(
        ResolvedHelmArtifact, [HelmArtifact.from_target(target)]
    )
    fetched_artifact = rule_runner.request(
        FetchedHelmArtifact,
        [
            FetchHelmArtifactRequest.from_target(
                target, description_of_origin="the test `test_fetch_single_artifact`"
            )
        ],
    )

    assert "Chart.yaml" in fetched_artifact.snapshot.files
    assert fetched_artifact.artifact == expected_resolved_artifact


def test_fetch_all_artifacts(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "3rdparty/helm/BUILD": dedent(
                """\
                helm_artifact(
                  name="cert-manager",
                  repository="https://charts.jetstack.io/",
                  artifact="cert-manager",
                  version="v1.7.1"
                )

                helm_artifact(
                    name="prometheus-stack",
                    repository="https://prometheus-community.github.io/helm-charts",
                    artifact="kube-prometheus-stack",
                    version="^27.2.0"
                )
                """
            ),
        }
    )

    targets = rule_runner.request(AllHelmArtifactTargets, [])

    expected_artifacts = [
        rule_runner.request(ResolvedHelmArtifact, [HelmArtifact.from_target(tgt)])
        for tgt in targets
    ]
    fetched_artifacts = rule_runner.request(AllFetchedHelmArtifacts, [])

    assert len(fetched_artifacts) == len(expected_artifacts)
    for fetched, expected in zip(fetched_artifacts, expected_artifacts):
        assert fetched.artifact == expected
        assert "Chart.yaml" in fetched.snapshot.files
