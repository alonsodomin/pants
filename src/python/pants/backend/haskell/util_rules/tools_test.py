# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import pytest

from pants.backend.haskell.util_rules.tools import CabalBinary, GhcBinary
from pants.backend.haskell.util_rules.tools import rules as tool_rules
from pants.testutil.rule_runner import PYTHON_BOOTSTRAP_ENV, QueryRule, RuleRunner


@pytest.fixture
def rule_runner() -> RuleRunner:
    return RuleRunner(rules=[*tool_rules(), QueryRule(GhcBinary, ()), QueryRule(CabalBinary, ())])


def test_find_tools(rule_runner: RuleRunner) -> None:
    rule_runner.set_options([], env_inherit=PYTHON_BOOTSTRAP_ENV)

    ghc = rule_runner.request(GhcBinary, [])
    assert ghc.path

    cabal = rule_runner.request(CabalBinary, [])
    assert cabal.path
