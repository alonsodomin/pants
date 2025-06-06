# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
from __future__ import annotations

from collections.abc import Iterable

from pants.backend.visibility.rule_types import BuildFileVisibilityRules
from pants.backend.visibility.subsystem import VisibilitySubsystem
from pants.engine.goal import CurrentExecutingGoals
from pants.engine.internals.build_files import get_dependencies_rule_application
from pants.engine.internals.dep_rules import (
    BuildFileDependencyRulesImplementation,
    BuildFileDependencyRulesImplementationRequest,
)
from pants.engine.rules import Rule, collect_rules, implicitly, rule
from pants.engine.target import (
    Dependencies,
    DependenciesRuleApplicationRequest,
    FieldSet,
    ValidatedDependencies,
    ValidateDependenciesRequest,
)
from pants.engine.unions import UnionRule


class BuildFileVisibilityImplementationRequest(BuildFileDependencyRulesImplementationRequest):
    pass


class VisibilityValidateFieldSet(FieldSet):
    required_fields = (Dependencies,)


class VisibilityValidateDependenciesRequest(ValidateDependenciesRequest):
    field_set_type = VisibilityValidateFieldSet


@rule
def build_file_visibility_implementation(
    _: BuildFileVisibilityImplementationRequest,
) -> BuildFileDependencyRulesImplementation:
    return BuildFileDependencyRulesImplementation(BuildFileVisibilityRules)


@rule
async def visibility_validate_dependencies(
    request: VisibilityValidateDependenciesRequest,
    visibility: VisibilitySubsystem,
    goals: CurrentExecutingGoals,
) -> ValidatedDependencies:
    if not visibility.enforce or goals.is_running("lint"):
        return ValidatedDependencies()

    address = request.field_set.address
    dependencies_rule_action = await get_dependencies_rule_application(
        DependenciesRuleApplicationRequest(
            address=address,
            dependencies=request.dependencies,
            description_of_origin=f"get dependency rules for {address}",
        ),
        **implicitly(),
    )
    dependencies_rule_action.execute_actions()
    return ValidatedDependencies()


def rules() -> Iterable[Rule | UnionRule]:
    return (
        *collect_rules(),
        UnionRule(
            BuildFileDependencyRulesImplementationRequest,
            BuildFileVisibilityImplementationRequest,
        ),
        UnionRule(ValidateDependenciesRequest, VisibilityValidateDependenciesRequest),
    )
