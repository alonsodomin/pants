# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from pants.backend.helm.target_types import (
    AllHelmArtifactTargets,
    HelmArtifactFieldSet,
    HelmArtifactRegistryField,
    HelmArtifactRepositoryField,
)
from pants.engine.addresses import Address
from pants.engine.rules import collect_rules, rule
from pants.engine.target import Target
from pants.util.frozendict import FrozenDict


class MissingHelmArtifactLocation(ValueError):
    def __init__(self, address: Address) -> None:
        super().__init__(
            f"Target at address '{address}' needs to specify either `{HelmArtifactRegistryField.alias}`, "
            f"`{HelmArtifactRepositoryField.alias}` or both."
        )


@dataclass(frozen=True)
class HelmArtifactRegistryLocation:
    registry: str
    repository: str | None


@dataclass(frozen=True)
class HelmArtifactClassicRepositoryLocation:
    repository: str


@dataclass(frozen=True)
class HelmArtifactMetadata:
    name: str
    version: str
    location: HelmArtifactRegistryLocation | HelmArtifactClassicRepositoryLocation


@dataclass(frozen=True)
class HelmArtifact:
    metadata: HelmArtifactMetadata
    address: Address

    @classmethod
    def from_target(cls, target: Target) -> HelmArtifact:
        return cls.from_field_set(HelmArtifactFieldSet.create(target))

    @classmethod
    def from_field_set(cls, field_set: HelmArtifactFieldSet) -> HelmArtifact:
        registry = field_set.registry.value
        repository = field_set.repository.value
        if not registry and not repository:
            raise MissingHelmArtifactLocation(field_set.address)

        registry_location: HelmArtifactRegistryLocation | None = None
        if registry:
            registry_location = HelmArtifactRegistryLocation(registry, repository)

        metadata = HelmArtifactMetadata(
            name=cast(str, field_set.artifact.value),
            version=cast(str, field_set.version.value),
            location=registry_location
            or HelmArtifactClassicRepositoryLocation(cast(str, repository)),
        )

        return cls(metadata=metadata, address=field_set.address)

    @property
    def name(self) -> str:
        return self.metadata.name

    @property
    def remote_spec(self) -> str:
        if isinstance(self.metadata.location, HelmArtifactRegistryLocation):
            repo_ref = f"{self.metadata.location.registry}/{self.metadata.location.repository or ''}".rstrip(
                "/"
            )
        else:
            repo_ref = self.metadata.location.repository
        return f"{repo_ref}/{self.metadata.name}"


class ThirdPartyArtifactMapping(FrozenDict[str, Address]):
    pass


@rule
def third_party_artifact_mapping(
    all_helm_artifact_tgts: AllHelmArtifactTargets,
) -> ThirdPartyArtifactMapping:
    artifacts = [HelmArtifact.from_target(tgt) for tgt in all_helm_artifact_tgts]
    return ThirdPartyArtifactMapping(
        {artifact.remote_spec: artifact.address for artifact in artifacts}
    )


def rules():
    return collect_rules()
