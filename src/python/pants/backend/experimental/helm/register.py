# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.helm.goals import deploy, lint, package, publish, tailor
from pants.backend.helm.dependency_inference import deployment
from pants.backend.helm.target_types import (
    HelmArtifactTarget,
    HelmChartTarget,
    HelmDeploymentTarget,
    HelmUnitTestTestsGeneratorTarget,
    HelmUnitTestTestTarget,
)
from pants.backend.helm.target_types import rules as target_types_rules
from pants.backend.helm.test.unittest import rules as test_rules
from pants.backend.helm.util_rules import chart, sources, tool


def target_types():
    return [
        HelmArtifactTarget,
        HelmChartTarget,
        HelmDeploymentTarget,
        HelmUnitTestTestTarget,
        HelmUnitTestTestsGeneratorTarget,
    ]


def rules():
    return [
        *chart.rules(),
        *lint.rules(),
        *deploy.rules(),
        *deployment.rules(),
        *package.rules(),
        *publish.rules(),
        *tailor.rules(),
        *test_rules(),
        *sources.rules(),
        *tool.rules(),
        *target_types_rules(),
    ]
