# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.codegen import export_codegen_goal
from pants.backend.codegen.soap import tailor
from pants.backend.codegen.soap.java.rules import rules as java_rules
from pants.backend.codegen.soap.target_types import WsdlSourcesGeneratorTarget, WsdlSourceTarget
from pants.backend.codegen.soap.target_types import rules as target_types_rules
from pants.core.util_rules import stripped_source_files


def target_types():
    return [WsdlSourceTarget, WsdlSourcesGeneratorTarget]


def rules():
    return [
        *java_rules(),
        *tailor.rules(),
        *export_codegen_goal.rules(),
        *target_types_rules(),
        *stripped_source_files.rules(),
    ]
