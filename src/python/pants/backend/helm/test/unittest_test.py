# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from textwrap import dedent

import pytest

from pants.backend.helm.dependency_inference.unittest import rules as unittest_dependency_rules
from pants.backend.helm.target_types import HelmChartTarget, HelmUnitTestTestTarget
from pants.backend.helm.target_types import rules as target_types_rules
from pants.backend.helm.test.unittest import HelmUnitTestFieldSet
from pants.backend.helm.test.unittest import rules as test_rules
from pants.backend.helm.testutil import (
    HELM_CHART_FILE,
    HELM_TEMPLATE_HELPERS_FILE,
    HELM_VALUES_FILE,
    K8S_SERVICE_FILE,
)
from pants.backend.helm.util_rules import chart, tool
from pants.core.goals.test import TestResult
from pants.core.util_rules import external_tool, stripped_source_files
from pants.engine.addresses import Address
from pants.engine.rules import QueryRule
from pants.source.source_root import rules as source_root_rules
from pants.testutil.rule_runner import RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(
        target_types=[HelmChartTarget, HelmUnitTestTestTarget],
        rules=[
            *external_tool.rules(),
            *tool.rules(),
            *chart.rules(),
            *test_rules(),
            *stripped_source_files.rules(),
            *source_root_rules(),
            *unittest_dependency_rules(),
            *target_types_rules(),
            QueryRule(TestResult, (HelmUnitTestFieldSet,)),
        ],
    )


def test_simple_success(rule_runner: RuleRunner) -> None:
    rule_runner.write_files(
        {
            "BUILD": "helm_chart(name='mychart')",
            "Chart.yaml": HELM_CHART_FILE,
            "values.yaml": HELM_VALUES_FILE,
            "templates/_helpers.tpl": HELM_TEMPLATE_HELPERS_FILE,
            "templates/service.yaml": K8S_SERVICE_FILE,
            "tests/BUILD": "helm_unittest_test(name='test', source='service_test.yaml')",
            "tests/service_test.yaml": dedent(
                """\
                suite: test service
                templates:
                  - service.yaml
                values:
                  - ../values.yaml
                tests:
                  - it: should work
                    asserts:
                      - isKind:
                          of: Service
                      - equal:
                          path: spec.type
                          value: ClusterIP
                """
            ),
        }
    )

    target = rule_runner.get_target(Address("tests", target_name="test"))
    field_set = HelmUnitTestFieldSet.create(target)

    result = rule_runner.request(TestResult, [field_set])

    assert result.exit_code == 0
    assert result.xml_results and result.xml_results.files
