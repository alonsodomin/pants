# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
import re
from abc import ABCMeta
from dataclasses import dataclass
from itertools import chain
from pathlib import Path
from typing import ClassVar, Iterable, Tuple, Type, TypeVar

from pants.core.util_rules.system_binaries import (
    BinaryNotFoundError,
    BinaryPath,
    BinaryPathRequest,
    BinaryPaths,
    BinaryPathTest,
    SearchPath,
)
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest
from pants.engine.process import Process, ProcessCacheScope, ProcessResult
from pants.engine.rules import Get, MultiGet, collect_rules, rule
from pants.option.option_types import StrListOption, StrOption
from pants.option.subsystem import Subsystem
from pants.util.frozendict import FrozenDict
from pants.util.logging import LogLevel
from pants.util.memo import memoized_method
from pants.util.strutil import bullet_list, softwrap

DEFAULT_SEARCH_PATH = ["<GHCUP>", "<PATH>"]


@dataclass(frozen=True)
class HaskellToolRequest:
    tool_name: str
    minimum_version: str
    search_path: tuple[str, ...]
    test_args: tuple[str, ...]
    test_version_pattern: str


class HaskellToolBase(Subsystem, metaclass=ABCMeta):
    default_minimum_version: ClassVar[str]
    default_test_version_pattern: ClassVar[str] = "version (\\d+\\.\\d+(\\.\\d+)*)"
    default_test_args: ClassVar[Tuple[str, ...]] = ("--version",)
    default_search_paths: ClassVar[Tuple[str, ...]] = tuple(DEFAULT_SEARCH_PATH)

    search_paths = StrListOption(
        "--search-paths",
        default=lambda cls: list(cls.default_search_paths),
        help=lambda cls: f"Search path for Haskell `{cls.options_scope}` tool.",
    )

    minimum_version = StrOption(
        "--minimum-expected-version",
        default=lambda cls: cls.default_minimum_version,
        help=lambda cls: softwrap(
            f"""
            The minimum version for Haskell `{cls.options_scope}` tool discovered by Pants must support.

            For example, if you set `'{cls.default_minimum_version}'`, then Pants will look for a {cls.options_scope} binary that is {cls.default_minimum_version}+,
            e.g. 1.17 or 1.18.

            You should still set the Go version for each module in your `go.mod` with the `go`
            directive.

            Do not include the patch version.
            """
        ),
    )
    test_args = StrListOption(
        "--test-args",
        default=lambda cls: list(cls.default_test_args),
        help=lambda cls: f"Arguments to use during discovery of the Haskell {cls.options_scope} tool.",
    )
    test_version_pattern = StrOption(
        "--test-version-pattern",
        default=lambda cls: cls.default_test_version_pattern,
        help=lambda cls: f"Regular expression to be used to determine the version of the Haskell {cls.options_scope} tool.",
    )

    @memoized_method
    def to_request(self) -> HaskellToolRequest:
        return HaskellToolRequest(
            tool_name=self.options_scope,
            minimum_version=self.minimum_version,
            search_path=self.search_paths,
            test_args=self.test_args,
            test_version_pattern=self.test_version_pattern,
        )


def _expand_search_paths(search_paths: Iterable[str], env: EnvironmentVars) -> list[str]:
    special_strings = {
        "<PATH>": lambda: get_environment_paths(env),
        "<GHCUP>": lambda: get_ghcup_paths(env),
    }

    expanded = []
    for s in search_paths:
        if s in special_strings:
            special_paths = special_strings[s]()
            expanded.extend(special_paths)
        else:
            expanded.append(s)

    return expanded


def get_environment_paths(env: EnvironmentVars) -> list[str]:
    """Returns a list of paths specified by the PATH env var."""
    pathstr = env.get("PATH")
    if pathstr:
        return pathstr.split(os.pathsep)
    return []


def get_ghcup_paths(env: EnvironmentVars) -> list[str]:
    """Returns a list of paths to Haskell tools managed by ghcup."""
    ghcup_root = get_ghcup_root(env)
    if not ghcup_root:
        return []

    ghc_dir = Path(ghcup_root, "ghc")
    if not ghc_dir.is_dir():
        return []

    paths = []
    for version in sorted(ghc_dir.iterdir()):
        path = Path(ghc_dir, version, "bin")
        if path.is_dir():
            paths.append(str(path))
    return paths


def get_ghcup_root(env: EnvironmentVars) -> str | None:
    """See https://www.haskell.org/ghcup/install/#manual-install."""
    home_from_env = env.get("HOME")
    if home_from_env:
        return os.path.join(home_from_env, ".ghcup")
    return None


_HaskellBinary = TypeVar("_HaskellBinary", bound="HaskellBinary")


@dataclass(frozen=True)
class _HaskellToolPath:
    binary_path: BinaryPath
    version: str
    env: FrozenDict[str, str]


@dataclass(frozen=True)
class HaskellBinary(metaclass=ABCMeta):
    _internal_path: _HaskellToolPath

    @classmethod
    def from_haskell_path(cls: Type[_HaskellBinary], path: _HaskellToolPath) -> _HaskellBinary:
        return cls(_internal_path=path)

    @property
    def binary_path(self) -> BinaryPath:
        return self._internal_path.binary_path

    @property
    def version(self) -> str:
        return self._internal_path.version

    @property
    def env(self) -> FrozenDict[str, str]:
        return self._internal_path.env


class GhcBinary(HaskellBinary):
    """The Glasgow Haskell Compiler (GHC) tool."""


class GhcSubsystem(HaskellToolBase):
    options_scope = "ghc"
    help = "The Glasglow Haskell compiler"

    default_minimum_version = "8.10"


class CabalBinary(HaskellBinary):
    """The Cabal build tool."""


class CabalSubsystem(HaskellToolBase):
    options_scope = "cabal"
    help = "The Haskell Cabal tool"

    default_minimum_version = "3.6"


@rule
async def find_haskell_tool(req: HaskellToolRequest) -> _HaskellToolPath:
    env_vars = await Get(EnvironmentVars, EnvironmentVarsRequest(["HOME", "PATH"]))
    request = BinaryPathRequest(
        binary_name=req.tool_name,
        search_path=SearchPath(_expand_search_paths(req.search_path, env_vars)),
        test=BinaryPathTest(args=req.test_args),
    )
    found_paths = await Get(BinaryPaths, BinaryPathRequest, request)

    test_version_results = await MultiGet(
        Get(
            ProcessResult,
            Process(
                [bin_path.path, *req.test_args],
                description=f"Determine version of Haskell tool {bin_path.path}",
                level=LogLevel.DEBUG,
                cache_scope=ProcessCacheScope.PER_RESTART_SUCCESSFUL,
            ),
        )
        for bin_path in found_paths.paths
    )

    def parse_version(v: str) -> tuple[int, int]:
        major, minor = v.split(".", maxsplit=1)
        return int(major), int(minor)

    invalid_versions = []
    for bin_path, version_result in zip(found_paths.paths, test_version_results):
        version_regex = re.compile(req.test_version_pattern)
        matched_version = list(
            chain.from_iterable(version_regex.findall(version_result.stdout.decode("utf-8")))
        )
        matched_version_components = matched_version[0].split(".")

        found_version = f"{matched_version_components[0]}.{matched_version_components[1]}"

        is_compatible = (found_version == req.minimum_version) or (
            parse_version(req.minimum_version) <= parse_version(found_version)
        )
        if is_compatible:
            return _HaskellToolPath(binary_path=bin_path, version=found_version, env=env_vars)

        invalid_versions.append((bin_path.path, found_version))

    invalid_versions_str = bullet_list(
        f"{path}: {version}" for path, version in sorted(invalid_versions)
    )
    raise BinaryNotFoundError(
        softwrap(
            f"""
            Cannot find a `{req.tool_name}` binary compatible with the minimum version of
            {req.minimum_version} (set by `[{req.tool_name}].minimum_expected_version`).

            Found these `{req.tool_name}` binaries but they have incompatible versions:

            {invalid_versions_str}

            To fix, please install the expected version or newer (https://golang.org/doc/install)
            and ensure that it is discoverable via the option `[{req.tool_name}].search_paths`, or change
            `[{req.tool_name}].expected_minimum_version`.
            """
        )
    )


@rule
async def find_ghc(subsystem: GhcSubsystem) -> GhcBinary:
    bin_path = await Get(_HaskellToolPath, HaskellToolRequest, subsystem.to_request())
    return GhcBinary.from_haskell_path(bin_path)


@rule
async def find_cabal(subsystem: CabalSubsystem) -> CabalBinary:
    bin_path = await Get(_HaskellToolPath, HaskellToolRequest, subsystem.to_request())
    return CabalBinary.from_haskell_path(bin_path)


def rules():
    return collect_rules()
