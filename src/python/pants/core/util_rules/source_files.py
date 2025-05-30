# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import PurePath

from pants.engine.fs import MergeDigests, Snapshot
from pants.engine.internals.graph import hydrate_sources
from pants.engine.intrinsics import digest_to_snapshot
from pants.engine.rules import collect_rules, concurrently, implicitly, rule
from pants.engine.target import HydrateSourcesRequest, SourcesField, Target


@dataclass(frozen=True)
class SourceFiles:
    """A merged snapshot of the `sources` fields of multiple targets."""

    snapshot: Snapshot

    # The subset of files in snapshot that are not intended to have an associated source root.
    # That is, the sources of files() targets.
    unrooted_files: tuple[str, ...]

    @property
    def files(self) -> tuple[str, ...]:
        return self.snapshot.files


@dataclass(frozen=True)
class SourceFilesRequest:
    sources_fields: tuple[SourcesField, ...]
    for_sources_types: tuple[type[SourcesField], ...]
    enable_codegen: bool

    def __init__(
        self,
        sources_fields: Iterable[SourcesField],
        *,
        for_sources_types: Iterable[type[SourcesField]] = (SourcesField,),
        enable_codegen: bool = False,
    ) -> None:
        object.__setattr__(self, "sources_fields", tuple(sources_fields))
        object.__setattr__(self, "for_sources_types", tuple(for_sources_types))
        object.__setattr__(self, "enable_codegen", enable_codegen)


@rule(desc="Get all relevant source files")
async def determine_source_files(request: SourceFilesRequest) -> SourceFiles:
    """Merge all `SourceBaseField`s into one Snapshot."""
    unrooted_files: set[str] = set()
    all_hydrated_sources = await concurrently(
        hydrate_sources(
            HydrateSourcesRequest(
                sources_field,
                for_sources_types=request.for_sources_types,
                enable_codegen=request.enable_codegen,
            ),
            **implicitly(),
        )
        for sources_field in request.sources_fields
    )

    for hydrated_sources, sources_field in zip(all_hydrated_sources, request.sources_fields):
        if not sources_field.uses_source_roots:
            unrooted_files.update(hydrated_sources.snapshot.files)

    digests_to_merge = tuple(
        hydrated_sources.snapshot.digest for hydrated_sources in all_hydrated_sources
    )
    result = await digest_to_snapshot(**implicitly(MergeDigests(digests_to_merge)))
    return SourceFiles(result, tuple(sorted(unrooted_files)))


@dataclass(frozen=True)
class ClassifiedSources:
    target_type: type[Target]
    files: Collection[str]
    name: str | None = None


def classify_files_for_sources_and_tests(
    paths: Iterable[str],
    test_file_glob: tuple[str, ...],
    sources_generator: type[Target],
    tests_generator: type[Target],
) -> Iterable[ClassifiedSources]:
    """Classify files when running the tailor goal logic.

    This function is to be re-used by any tailor related code that needs to separate sources
    collected for the target generators to own sources code (`language-name_sources` targets) and
    tests code (`language-name_tests` targets).
    """
    sources_files = set(paths)
    test_files = {
        path for path in paths if any(PurePath(path).match(glob) for glob in test_file_glob)
    }
    if sources_files:
        yield ClassifiedSources(sources_generator, files=sources_files - test_files)
    if test_files:
        yield ClassifiedSources(tests_generator, test_files, "tests")


def rules():
    return collect_rules()
