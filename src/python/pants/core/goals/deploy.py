# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations
from collections import deque

import logging
from abc import ABCMeta
from dataclasses import dataclass
from itertools import chain
from typing import Iterable, Iterator
import asyncio

from pants.core.goals.package import PackageFieldSet
from pants.core.goals.publish import PublishFieldSet, PublishProcesses, PublishProcessesRequest
from pants.engine.console import Console
from pants.engine.addresses import Address
from pants.engine.environment import EnvironmentName
from pants.engine.goal import Goal, GoalSubsystem
from pants.engine.process import InteractiveProcess, InteractiveProcessResult
from pants.engine.rules import Effect, Get, MultiGet, collect_rules, goal_rule, rule
from pants.engine.target import (
    Dependencies,
    DependenciesRequest,
    FieldSet,
    FieldSetsPerTarget,
    FieldSetsPerTargetRequest,
    NoApplicableTargetsBehavior,
    Target,
    TargetRootsToFieldSets,
    TargetRootsToFieldSetsRequest,
    Targets,
)
from pants.engine.unions import union
from pants.util.strutil import pluralize

logger = logging.getLogger(__name__)


@union(in_scope_types=[EnvironmentName])
@dataclass(frozen=True)
class DeployFieldSet(FieldSet, metaclass=ABCMeta):
    """The FieldSet type for the `deploy` goal.

    Union members may list any fields required to fulfill the instantiation of the `DeployProcess`
    result of the deploy rule.
    """


@dataclass(frozen=True)
class DeployProcess:
    """A process that when executed will have the side effect of deploying a target.

    To provide with the ability to deploy a given target, create a custom `DeployFieldSet` for
    that given target and implement a rule that returns `DeployProcess` for that custom field set:

    Example:

        @dataclass(frozen=True)
        class MyDeploymentFieldSet(DeployFieldSet):
            pass

        @rule
        async def my_deployment_process(field_set: MyDeploymentFieldSet) -> DeployProcess:
            # Create the underlying process that executes the deployment
            process = Process(...)
            return DeployProcess(
                name="my_deployment",
                process=InteractiveProcess.from_process(process)
            )

        def rules():
            return [
                *collect_rules(),
                UnionRule(DeployFieldSet, MyDeploymentFieldSet)
            ]

    Use the `publish_dependencies` field to provide with a list of targets that produce packages
    which need to be externally published before the deployment process is executed.
    """

    name: str
    process: InteractiveProcess | None
    publish_dependencies: tuple[Target, ...] = ()
    description: str | None = None


class DeploySubsystem(GoalSubsystem):
    name = "experimental-deploy"
    help = "Perform a deployment process."

    required_union_implementation = (DeployFieldSet,)


@dataclass(frozen=True)
class Deploy(Goal):
    subsystem_cls = DeploySubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY  # TODO(#17129) — Migrate this.


@dataclass(frozen=True)
class _PublishProcessesForTargetRequest:
    target: Target


@rule
async def publish_process_for_target(
    request: _PublishProcessesForTargetRequest,
) -> PublishProcesses:
    package_field_sets, publish_field_sets = await MultiGet(
        Get(FieldSetsPerTarget, FieldSetsPerTargetRequest(PackageFieldSet, [request.target])),
        Get(FieldSetsPerTarget, FieldSetsPerTargetRequest(PublishFieldSet, [request.target])),
    )

    return await Get(
        PublishProcesses,
        PublishProcessesRequest(
            package_field_sets=package_field_sets.field_sets,
            publish_field_sets=publish_field_sets.field_sets,
        ),
    )


async def _all_publish_processes(targets: Iterable[Target]) -> PublishProcesses:
    processes_per_target = await MultiGet(
        Get(PublishProcesses, _PublishProcessesForTargetRequest(target)) for target in targets
    )

    return PublishProcesses(chain.from_iterable(processes_per_target))


async def _invoke_process(
    console: Console,
    process: InteractiveProcess | None,
    *,
    names: Iterable[str],
    success_status: str,
    description: str | None = None,
) -> tuple[int, tuple[str, ...]]:
    results = []

    if not process:
        sigil = console.sigil_skipped()
        status = "skipped"
        if description:
            status += f" {description}"
        for name in names:
            results.append(f"{sigil} {name} {status}.")
        return 0, tuple(results)

    logger.debug(f"Execute {process}")
    res = await Effect(InteractiveProcessResult, InteractiveProcess, process)
    if res.exit_code == 0:
        sigil = console.sigil_succeeded()
        status = success_status
        prep = "to"
    else:
        sigil = console.sigil_failed()
        status = "failed"
        prep = "for"

    if description:
        status += f" {prep} {description}"

    for name in names:
        results.append(f"{sigil} {name} {status}")

    return res.exit_code, tuple(results)


@dataclass(frozen=True)
class _DeployStep:
    address: Address
    process: DeployProcess
    depends_on: tuple[_DeployStep, ...]

    def closure(self) -> Iterator[_DeployStep]:
        visited = set()
        queue = deque(self.depends_on)

        while queue:
            step = deque.popleft()
            if step.address in visited:
                continue
            visited.add(step.address)
            yield step
            queue.extend(step.depends_on)
        yield self

    def publish_dependencies(self) -> Iterator[Target]:
        for step in self.closure():
            yield from step.process.publish_dependencies

@dataclass(frozen=True)
class _FallibleDeployStepResult:
    exit_code: int
    results: tuple[str, ...]

async def _build_deploy_graph(target: Target) -> _DeployStep:
    process, dependencies = await MultiGet(
        Get(DeployProcess, DeployFieldSet, DeployFieldSet.create(target)),
        Get(Targets, DependenciesRequest(target[Dependencies]))
    )

    deployable_dependencies = [tgt for tgt in dependencies if DeployFieldSet.is_applicable(tgt)]
    depends_on = await asyncio.gather(*[_build_deploy_graph(tgt) for tgt in deployable_dependencies])
    return _DeployStep(target.address, process, tuple(depends_on))

async def _run_deploy_steps(steps: Iterable[_DeployStep], console: Console) -> _FallibleDeployStepResult:
    if len(steps) > 0:
        dependency_results = await asyncio.gather(*[_run_deploy_step(step, console) for step in steps])
        exit_code = max(result.exit_code for result in dependency_results)
        results = [chain.from_iterable(result.results) for result in dependency_results]
        return _FallibleDeployStepResult(exit_code, results)
    else:
        return _FallibleDeployStepResult(0, [])

async def _run_deploy_step(step: _DeployStep, console: Console) -> _FallibleDeployStepResult:
    dependencies_result = await _run_deploy_steps(step.depends_on, console)

    exit_code = dependencies_result.exit_code
    results = dependencies_result.results
    
    if exit_code == 0:
        # Invoke the deployment.
        ec, statuses = await _invoke_process(
            console,
            step.deploy.process,
            names=[step.deploy.name],
            success_status="deployed",
            description=step.deploy.description,
        )
        exit_code = ec if ec != 0 else exit_code
        results.extend(statuses)

    return _FallibleDeployStepResult(exit_code, results)

@goal_rule
async def run_deploy(console: Console, deploy_subsystem: DeploySubsystem) -> Deploy:
    target_roots_to_deploy_field_sets = await Get(
        TargetRootsToFieldSets,
        TargetRootsToFieldSetsRequest(
            DeployFieldSet,
            goal_description=f"the `{deploy_subsystem.name}` goal",
            no_applicable_targets_behavior=NoApplicableTargetsBehavior.error,
        ),
    )

    deployment_steps = [await _build_deploy_graph(tgt) for tgt in target_roots_to_deploy_field_sets.targets]
    publish_dependencies = chain.from_iterable(step.publish_dependencies for step in deployment_steps)

    exit_code = 0
    results = []
    if publish_dependencies:
        logger.info(f"Publishing {pluralize(len(publish_dependencies), 'dependency')} ...")

        # Publish all deployment dependencies first.
        publish_processes = await _all_publish_processes(publish_dependencies)
        for publish in publish_processes:
            ec, statuses = await _invoke_process(
                console,
                publish.process,
                names=publish.names,
                description=publish.description,
                success_status="published",
            )
            exit_code = ec if ec != 0 else exit_code
            results.extend(statuses)

    if exit_code == 0:
        logger.info("Deploying targets...")
        deployment_result = await _run_deploy_steps(deployment_steps, console)
        exit_code = deployment_result.exit_code
        results.extend(deployment_result.results)

    console.print_stderr("")
    if not results:
        sigil = console.sigil_skipped()
        console.print_stderr(f"{sigil} Nothing deployed.")

    for line in results:
        console.print_stderr(line)

    return Deploy(deployment_result.exit_code)


def rules():
    return collect_rules()
