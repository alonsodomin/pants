# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from pants.engine.target import COMMON_TARGET_FIELDS, Dependencies, MultipleSourcesField, SingleSourceField, Target, TargetFilesGenerator


class HaskellSourceField(SingleSourceField):
  expected_file_extensions = (".hs",)

class HaskellDependenciesField(Dependencies):
  pass

class HaskellSourceTarget(Target):
  alias = "haskell_source"
  core_fields = (*COMMON_TARGET_FIELDS, HaskellSourceField, HaskellDependenciesField)
  help = "A single Haskell source file."

class HaskellGeneratingSourcesField(MultipleSourcesField):
  default = ("*.hs",)
  expected_file_extensions = ("*.hs",)

class HaskellSourcesGeneratorTarget(TargetFilesGenerator):
  alias = "haskell_sources"
  core_fields = (*COMMON_TARGET_FIELDS, HaskellGeneratingSourcesField, HaskellDependenciesField)
  generated_target_cls = HaskellSourceTarget
  copied_fields = COMMON_TARGET_FIELDS
  moved_fields = (HaskellDependenciesField,)
  help = f"Generates a `{HaskellSourceTarget.alias}` for each file in the `{HaskellGeneratingSourcesField.alias}` field."