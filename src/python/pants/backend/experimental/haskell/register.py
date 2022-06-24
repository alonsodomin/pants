# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.backend.haskell.target_types import HaskellSourceTarget, HaskellSourcesGeneratorTarget


def target_types():
  return [HaskellSourceTarget, HaskellSourcesGeneratorTarget]

def rules():
  return []