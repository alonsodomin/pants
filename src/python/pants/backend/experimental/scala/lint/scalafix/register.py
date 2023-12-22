# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.experimental.scala.register import rules as all_scala_rules
from pants.backend.scala.lint.scalafix import rules as scalafix_rules
from pants.backend.scala.compile.semanticdb.rules import rules as semanticdb_rules
from pants.backend.scala.lint.scalafix import skip_field


def rules():
    return [
        *all_scala_rules(),
        *semanticdb_rules(),
        *scalafix_rules.rules(),
        *skip_field.rules(),
    ]
