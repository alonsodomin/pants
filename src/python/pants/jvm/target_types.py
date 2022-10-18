# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

from abc import ABC, ABCMeta, abstractmethod
from dataclasses import dataclass
from typing import ClassVar, Iterable, Optional, Tuple

from pants.build_graph.build_file_aliases import BuildFileAliases
from pants.core.goals.generate_lockfiles import UnrecognizedResolveNamesError
from pants.core.goals.package import OutputPathField
from pants.core.goals.run import RestartableField
from pants.core.goals.test import TestExtraEnvVarsField, TestTimeoutField
from pants.engine.addresses import Address
from pants.engine.rules import collect_rules, rule
from pants.engine.target import (
    COMMON_TARGET_FIELDS,
    AsyncFieldMixin,
    Dependencies,
    FieldDefaultFactoryRequest,
    FieldDefaultFactoryResult,
    FieldSet,
    InvalidFieldException,
    InvalidTargetException,
    OptionalSingleSourceField,
    SequenceField,
    SingleSourceField,
    SpecialCasedDependencies,
    StringField,
    StringSequenceField,
    Target,
)
from pants.engine.unions import UnionRule
from pants.jvm.subsystems import JvmSubsystem
from pants.util.docutil import git_url
from pants.util.strutil import bullet_list, pluralize, softwrap

# -----------------------------------------------------------------------------------------------
# Generic resolve support fields
# -----------------------------------------------------------------------------------------------


class JvmDependenciesField(Dependencies):
    pass


class JvmResolveField(StringField, AsyncFieldMixin):
    alias = "resolve"
    required = False
    help = softwrap(
        """
        The resolve from `[jvm].resolves` to use when compiling this target.

        If not defined, will default to `[jvm].default_resolve`.
        """
        # TODO: Document expectations for dependencies once we validate that.
    )

    def normalized_value(self, jvm_subsystem: JvmSubsystem) -> str:
        """Get the value after applying the default and validating that the key is recognized."""
        resolve = self.value or jvm_subsystem.default_resolve
        if resolve not in jvm_subsystem.resolves:
            raise UnrecognizedResolveNamesError(
                [resolve],
                jvm_subsystem.resolves.keys(),
                description_of_origin=f"the field `{self.alias}` in the target {self.address}",
            )
        return resolve


class JvmJdkField(StringField):
    alias = "jdk"
    required = False
    help = softwrap(
        """
        The major version of the JDK that this target should be built with. If not defined,
        will default to `[jvm].default_source_jdk`.
        """
    )


class PrefixedJvmJdkField(JvmJdkField):
    alias = "jvm_jdk"


class PrefixedJvmResolveField(JvmResolveField):
    alias = "jvm_resolve"


# -----------------------------------------------------------------------------------------------
# `jvm_artifact` targets
# -----------------------------------------------------------------------------------------------

_DEFAULT_PACKAGE_MAPPING_URL = git_url(
    "src/python/pants/jvm/dependency_inference/jvm_artifact_mappings.py"
)


class JvmArtifactGroupField(StringField):
    alias = "group"
    required = True
    value: str
    help = softwrap(
        """
        The 'group' part of a Maven-compatible coordinate to a third-party JAR artifact.

        For the JAR coordinate `com.google.guava:guava:30.1.1-jre`, the group is `com.google.guava`.
        """
    )


class JvmArtifactArtifactField(StringField):
    alias = "artifact"
    required = True
    value: str
    help = softwrap(
        """
        The 'artifact' part of a Maven-compatible coordinate to a third-party JAR artifact.

        For the JAR coordinate `com.google.guava:guava:30.1.1-jre`, the artifact is `guava`.
        """
    )


class JvmArtifactVersionField(StringField):
    alias = "version"
    required = True
    value: str
    help = softwrap(
        """
        The 'version' part of a Maven-compatible coordinate to a third-party JAR artifact.

        For the JAR coordinate `com.google.guava:guava:30.1.1-jre`, the version is `30.1.1-jre`.
        """
    )


class JvmArtifactUrlField(StringField):
    alias = "url"
    required = False
    help = softwrap(
        """
        A URL that points to the location of this artifact.

        If specified, Pants will not fetch this artifact from default Maven repositories, and
        will instead fetch the artifact from this URL. To use default maven
        repositories, do not set this value.

        Note that `file:` URLs are not supported. Instead, use the `jar` field for local
        artifacts.
        """
    )


class JvmArtifactJarSourceField(OptionalSingleSourceField):
    alias = "jar"
    expected_file_extensions = (".jar",)
    help = softwrap(
        """
        A local JAR file that provides this artifact to the lockfile resolver, instead of a
        Maven repository.

        Path is relative to the BUILD file.

        Use the `url` field for remote artifacts.
        """
    )

    @classmethod
    def compute_value(cls, raw_value: Optional[str], address: Address) -> Optional[str]:
        value_or_default = super().compute_value(raw_value, address)
        if value_or_default and value_or_default.startswith("file:"):
            raise InvalidFieldException(
                softwrap(
                    f"""
                    The `{cls.alias}` field does not support `file:` URLS, but the target
                    {address} sets the field to `{value_or_default}`.

                    Instead, use the `jar` field to specify the relative path to the local jar file.
                    """
                )
            )
        return value_or_default


class JvmArtifactPackagesField(StringSequenceField):
    alias = "packages"
    help = softwrap(
        f"""
        The JVM packages this artifact provides for the purposes of dependency inference.

        For example, the JVM artifact `junit:junit` might provide `["org.junit.**"]`.

        Usually you can leave this field off. If unspecified, Pants will fall back to the
        `[java-infer].third_party_import_mapping`, then to a built in mapping
        ({_DEFAULT_PACKAGE_MAPPING_URL}), and then finally it will default to
        the normalized `group` of the artifact. For example, in the absence of any other mapping
        the artifact `io.confluent:common-config` would default to providing
        `["io.confluent.**"]`.

        The package path may be made recursive to match symbols in subpackages
        by adding `.**` to the end of the package path. For example, specify `["org.junit.**"]`
        to infer a dependency on the artifact for any file importing a symbol from `org.junit` or
        its subpackages.
        """
    )


class JvmProvidesTypesField(StringSequenceField):
    alias = "experimental_provides_types"
    help = softwrap(
        """
        Signals that the specified types should be fulfilled by these source files during
        dependency inference.

        This allows for specific types within packages that are otherwise inferred as
        belonging to `jvm_artifact` targets to be unambiguously inferred as belonging
        to this first-party source.

        If a given type is defined, at least one source file captured by this target must
        actually provide that symbol.
        """
    )


class JvmArtifactExcludeDependenciesField(StringSequenceField):
    alias = "excludes"
    help = softwrap(
        """
        A list of unversioned coordinates (i.e. `group:artifact`) that should be excluded
        as dependencies when this artifact is resolved.

        This does not prevent this artifact from being included in the resolve as a dependency
        of other artifacts that depend on it, and is currently intended as a way to resolve
        version conflicts in complex resolves.

        These values are passed directly to Coursier, and if specified incorrectly will show a
        parse error from Coursier.
        """
    )


class JvmArtifactResolveField(JvmResolveField):
    help = softwrap(
        """
        The resolve from `[jvm].resolves` that this artifact should be included in.

        If not defined, will default to `[jvm].default_resolve`.

        When generating a lockfile for a particular resolve via the `coursier-resolve` goal,
        it will include all artifacts that are declared compatible with that resolve. First-party
        targets like `java_source` and `scala_source` also declare which resolve they use
        via the `resolve` field; so, for your first-party code to use
        a particular `jvm_artifact` target, that artifact must be included in the resolve
        used by that code.
        """
    )


@dataclass(frozen=True)
class JvmArtifactFieldSet(FieldSet):
    group: JvmArtifactGroupField
    artifact: JvmArtifactArtifactField
    version: JvmArtifactVersionField
    packages: JvmArtifactPackagesField
    url: JvmArtifactUrlField

    required_fields = (
        JvmArtifactGroupField,
        JvmArtifactArtifactField,
        JvmArtifactVersionField,
        JvmArtifactPackagesField,
    )


class JvmArtifactTarget(Target):
    alias = "jvm_artifact"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        *JvmArtifactFieldSet.required_fields,
        JvmArtifactUrlField,  # TODO: should `JvmArtifactFieldSet` have an `all_fields` field?
        JvmArtifactJarSourceField,
        JvmArtifactResolveField,
        JvmArtifactExcludeDependenciesField,
    )
    help = softwrap(
        """
        A third-party JVM artifact, as identified by its Maven-compatible coordinate.

        That is, an artifact identified by its `group`, `artifact`, and `version` components.

        Each artifact is associated with one or more resolves (a logical name you give to a
        lockfile). For this artifact to be used by your first-party code, it must be
        associated with the resolve(s) used by that code. See the `resolve` field.
        """
    )

    def validate(self) -> None:
        if self[JvmArtifactJarSourceField].value and self[JvmArtifactUrlField].value:
            raise InvalidTargetException(
                f"You cannot specify both the `url` and `jar` fields, but both were set on the "
                f"`{self.alias}` target {self.address}."
            )


# -----------------------------------------------------------------------------------------------
# JUnit test support field(s)
# -----------------------------------------------------------------------------------------------


class JunitTestSourceField(SingleSourceField, metaclass=ABCMeta):
    """A marker that indicates that a source field represents a JUnit test."""


class JunitTestTimeoutField(TestTimeoutField):
    pass


class JunitTestExtraEnvVarsField(TestExtraEnvVarsField):
    pass


# -----------------------------------------------------------------------------------------------
# JAR support fields
# -----------------------------------------------------------------------------------------------


class JarShadingRule(ABC):
    alias: ClassVar[str]
    help: ClassVar[str]

    @abstractmethod
    def encode(self) -> str:
        pass


@dataclass(frozen=True)
class JarShadingRenameRule(JarShadingRule):
    alias = "shading_rename"
    help = "Renames all occurrences of the given `pattern` by the `replacement`."

    pattern: str
    replacement: str

    def encode(self) -> str:
        return f"rule {self.pattern} {self.replacement}"


@dataclass(frozen=True)
class JarShadingZapRule(JarShadingRule):
    alias = "shading_zap"
    help = "Removes from the final artifact the occurences of the `pattern`."

    pattern: str

    def encode(self) -> str:
        return f"zap {self.pattern}"


@dataclass(frozen=True)
class JarShadingKeepRule(JarShadingRule):
    alias = "shading_keep"
    help = softwrap(
        """
        Keeps in the final artifact the occurences of the `pattern`
        (and removes anything else).
        """
    )

    pattern: str

    def encode(self) -> str:
        return f"keep {self.pattern}"


JVM_JAR_SHADING_RULE_TYPES = [JarShadingRenameRule, JarShadingZapRule, JarShadingKeepRule]


class JvmJarShadingRules(SequenceField[JarShadingRule]):
    alias = "shading_rules"
    required = False
    expected_element_type = JarShadingRule
    expected_type_description = "an iterable of ShadingRule"
    help = softwrap(
        f"""
        Shading rules to be applied to the final JAR artifact.

        There are {pluralize(len(JVM_JAR_SHADING_RULE_TYPES), "possible shading rule")} available,
        which are as follows:
        {bullet_list([f'`{rule.alias}`: {rule.help}' for rule in JVM_JAR_SHADING_RULE_TYPES])}

        When defining shading rules, just add them this field using the rule alias and passing along
        the required parameters.
        """
    )

    @classmethod
    def compute_value(
        cls, raw_value: Optional[Iterable[JarShadingRule]], address: Address
    ) -> Optional[Tuple[JarShadingRule, ...]]:
        return super().compute_value(raw_value, address)


@dataclass(frozen=True)
class JarDuplicateRule:
    alias: ClassVar[str] = "duplicate_rule"
    valid_actions: ClassVar[tuple[str, ...]] = ("skip", "replace", "concat", "concat_text", "throw")

    pattern: str
    action: str

    def validate(self) -> str | None:
        if self.action not in JarDuplicateRule.valid_actions:
            return softwrap(
                f"""
                Value '{self.action}' for `action` associated with pattern
                '{self.pattern}' is not valid.

                It must be one of {list(JarDuplicateRule.valid_actions)}.
                """
            )
        return None

    def __repr__(self) -> str:
        return f"{self.alias}(pattern='{self.pattern}', action='{self.action}')"


class DeployJarDuplicatePolicyField(SequenceField[JarDuplicateRule]):
    alias = "duplicate_policy"
    help = softwrap(
        f"""
        A list of the rules to apply when duplicate file entries are found in the final
        assembled JAR file.

        When defining a duplicate policy, just add `duplicate_rule` directives to this
        field as follows:

        Example:

        ```
        duplicate_policy=[
            duplicate_rule(pattern="^META-INF/services", action="concat_text"),
            duplicate_rule(pattern="^reference\\.conf", action="concat_text"),
            duplicate_rule(pattern="^org/apache/commons", action="throw"),
        ]
        ```

        Where:

        * The `pattern` field is treated as a regular expression
        * The `action` field must be one of {list(JarDuplicateRule.valid_actions)}.

        Note that the order in which the rules are listed is relevant.
        """
    )
    required = False

    expected_element_type = JarDuplicateRule
    expected_type_description = "a list of JAR duplicate rules"

    default = (JarDuplicateRule(pattern="^META-INF/services/", action="concat_text"),)

    @classmethod
    def compute_value(
        cls, raw_value: Optional[Iterable[JarDuplicateRule]], address: Address
    ) -> Optional[Tuple[JarDuplicateRule, ...]]:
        value = super().compute_value(raw_value, address)
        if value:
            errors = []
            for duplicate_rule in value:
                err = duplicate_rule.validate()
                if err:
                    errors.append(err)

            if errors:
                raise InvalidFieldException(
                    softwrap(
                        f"""
                        Invalid value for `{DeployJarDuplicatePolicyField.alias}` field.
                        Found following errors:

                        {bullet_list(errors)}
                        """
                    )
                )
        return value

    def value_or_default(self) -> tuple[JarDuplicateRule, ...]:
        if self.value is not None:
            return self.value
        return self.default


class JvmMainClassNameField(StringField):
    alias = "main"
    required = True
    help = softwrap(
        """
        `.`-separated name of the JVM class containing the `main()` method to be called when
        executing this JAR.
        """
    )


class DeployJarTarget(Target):
    alias = "deploy_jar"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        JvmDependenciesField,
        OutputPathField,
        JvmMainClassNameField,
        JvmJdkField,
        JvmResolveField,
        DeployJarDuplicatePolicyField,
        RestartableField,
        JvmJarShadingRules,
    )
    help = softwrap(
        """
        A `jar` file with first and third-party code bundled for deploys.

        The JAR will contain class files for both first-party code and
        third-party dependencies, all in a common directory structure.
        """
    )


# -----------------------------------------------------------------------------------------------
# `jvm_war` targets
# -----------------------------------------------------------------------------------------------


class JvmWarDependenciesField(Dependencies):
    pass


class JvmWarDescriptorAddressField(SingleSourceField):
    alias = "descriptor"
    default = "web.xml"
    help = "Path to a file containing the descriptor (i.e., web.xml) for this WAR file. Defaults to `web.xml`."


class JvmWarContentField(SpecialCasedDependencies):
    alias = "content"
    help = softwrap(
        """
        A list of addresses to `resources` and `files` targets with content to place in the
        document root of this WAR file.
        """
    )


class JvmWarTarget(Target):
    alias = "jvm_war"
    core_fields = (
        *COMMON_TARGET_FIELDS,
        JvmResolveField,
        JvmWarContentField,
        JvmWarDependenciesField,
        JvmWarDescriptorAddressField,
        OutputPathField,
    )
    help = softwrap(
        """
        A JSR 154 "web application archive" (or "war") with first-party and third-party code bundled for
        deploys in Java Servlet containers.
        """
    )


# -----------------------------------------------------------------------------------------------
# Dynamic Field defaults
# -----------------------------------------------------------------------------------------------#


class JvmResolveFieldDefaultFactoryRequest(FieldDefaultFactoryRequest):
    field_type = JvmResolveField


@rule
def jvm_resolve_field_default_factory(
    request: JvmResolveFieldDefaultFactoryRequest,
    jvm: JvmSubsystem,
) -> FieldDefaultFactoryResult:
    return FieldDefaultFactoryResult(lambda f: f.normalized_value(jvm))


def rules():
    return [
        *collect_rules(),
        UnionRule(FieldDefaultFactoryRequest, JvmResolveFieldDefaultFactoryRequest),
    ]


def build_file_aliases():
    return BuildFileAliases(
        objects={
            JarDuplicateRule.alias: JarDuplicateRule,
            **{rule.alias: rule for rule in JVM_JAR_SHADING_RULE_TYPES}
        }
    )
