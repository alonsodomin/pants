# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

import csv
import logging
from collections import defaultdict
from textwrap import fill, indent

from pants.backend.project_info.dependents import DependentsRequest, find_dependents
from pants.backend.python.subsystems.setup import PythonSetup
from pants.backend.python.target_types import InterpreterConstraintsField
from pants.backend.python.util_rules.interpreter_constraints import InterpreterConstraints
from pants.engine.addresses import Addresses
from pants.engine.console import Console
from pants.engine.goal import Goal, GoalSubsystem, Outputting
from pants.engine.internals.graph import find_all_targets
from pants.engine.internals.graph import transitive_targets as transitive_targets_get
from pants.engine.rules import collect_rules, concurrently, goal_rule, implicitly
from pants.engine.target import RegisteredTargetTypes, TransitiveTargetsRequest
from pants.engine.unions import UnionMembership
from pants.option.option_types import BoolOption
from pants.util.docutil import bin_name
from pants.util.strutil import softwrap

logger = logging.getLogger(__name__)


class PyConstraintsSubsystem(Outputting, GoalSubsystem):
    name = "py-constraints"
    help = "Determine what Python interpreter constraints are used by files/targets."

    summary = BoolOption(
        default=False,
        help=softwrap(
            """
            Output a CSV summary of interpreter constraints for your whole repository. The
            headers are `Target`, `Constraints`, `Transitive Constraints`, `# Dependencies`,
            and `# Dependents`.

            This information can be useful when prioritizing a migration from one Python version to
            another (e.g. to Python 3). Use `# Dependencies` and `# Dependents` to help prioritize
            which targets are easiest to port (low # dependencies) and highest impact to port
            (high # dependents).

            Use a tool like Pandas or Excel to process the CSV. Use the option
            `--py-constraints-output-file=summary.csv` to write directly to a file.
            """
        ),
    )


class PyConstraintsGoal(Goal):
    subsystem_cls = PyConstraintsSubsystem
    environment_behavior = Goal.EnvironmentBehavior.LOCAL_ONLY


@goal_rule
async def py_constraints(
    addresses: Addresses,
    console: Console,
    py_constraints_subsystem: PyConstraintsSubsystem,
    python_setup: PythonSetup,
    registered_target_types: RegisteredTargetTypes,
    union_membership: UnionMembership,
) -> PyConstraintsGoal:
    if py_constraints_subsystem.summary:
        dependents_header = "# Dependents"
        if addresses:
            console.print_stderr(
                softwrap(
                    """
                    The `py-constraints --summary` goal does not take file/target arguments. Run
                    `help py-constraints` for more details.
                    """
                )
            )
            return PyConstraintsGoal(exit_code=1)

        all_targets = await find_all_targets()
        all_python_targets = tuple(
            t for t in all_targets if t.has_field(InterpreterConstraintsField)
        )

        constraints_per_tgt = [
            InterpreterConstraints.create_from_targets([tgt], python_setup)
            for tgt in all_python_targets
        ]

        transitive_targets_per_tgt = await concurrently(
            transitive_targets_get(TransitiveTargetsRequest([tgt.address]), **implicitly())
            for tgt in all_python_targets
        )
        transitive_constraints_per_tgt = [
            InterpreterConstraints.create_from_targets(transitive_targets.closure, python_setup)
            for transitive_targets in transitive_targets_per_tgt
        ]

        dependents_per_root = await concurrently(
            find_dependents(
                DependentsRequest([tgt.address], transitive=True, include_roots=False),
                **implicitly(),
            )
            for tgt in all_python_targets
        )

        data = [
            {
                "Target": tgt.address.spec,
                "Constraints": str(constraints),
                "Transitive Constraints": str(transitive_constraints),
                "# Dependencies": len(transitive_targets.dependencies),
                dependents_header: len(dependents),
            }
            for tgt, constraints, transitive_constraints, transitive_targets, dependents in zip(
                all_python_targets,
                constraints_per_tgt,
                transitive_constraints_per_tgt,
                transitive_targets_per_tgt,
                dependents_per_root,
            )
        ]

        with py_constraints_subsystem.output_sink(console) as stdout:
            writer = csv.DictWriter(
                stdout,
                fieldnames=[
                    "Target",
                    "Constraints",
                    "Transitive Constraints",
                    "# Dependencies",
                    dependents_header,
                ],
            )
            writer.writeheader()
            for entry in data:
                writer.writerow(entry)

        return PyConstraintsGoal(exit_code=0)

    transitive_targets = await transitive_targets_get(
        TransitiveTargetsRequest(addresses), **implicitly()
    )
    final_constraints = InterpreterConstraints.create_from_targets(
        transitive_targets.closure, python_setup
    )

    if not final_constraints:
        target_types_with_constraints = sorted(
            tgt_type.alias
            for tgt_type in registered_target_types.types
            if tgt_type.class_has_field(InterpreterConstraintsField, union_membership)
        )
        logger.warning(
            softwrap(
                f"""
                No Python files/targets matched for the `py-constraints` goal. All target types with
                Python interpreter constraints: {", ".join(target_types_with_constraints)}
                """
            )
        )
        return PyConstraintsGoal(exit_code=0)

    constraints_to_addresses = defaultdict(set)
    for tgt in transitive_targets.closure:
        constraints = InterpreterConstraints.create_from_targets([tgt], python_setup)
        if not constraints:
            continue
        constraints_to_addresses[constraints].add(tgt.address)

    with py_constraints_subsystem.output(console) as output_stdout:
        output_stdout(f"Final merged constraints: {final_constraints}\n")
        if len(addresses) > 1:
            merged_constraints_warning = softwrap(
                f"""
                (These are the constraints used if you were to depend on all of the input
                files/targets together, even though they may end up never being used together in
                the real world. Consider using a more precise query or running
                `{bin_name()} py-constraints --summary`.)\n
                """
            )
            output_stdout(indent(fill(merged_constraints_warning, 80), "  "))

        for constraint, addrs in sorted(constraints_to_addresses.items()):
            output_stdout(f"\n{constraint}\n")
            for addr in sorted(addrs):
                output_stdout(f"  {addr}\n")

    return PyConstraintsGoal(exit_code=0)


def rules():
    return collect_rules()
