# Copyright 2022 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from pants.core.util_rules.system_binaries import (
    BinaryPath,
    BinaryPathRequest,
    BinaryPaths,
    BinaryPathTest,
    SearchPath,
)
from pants.engine.environment import Environment, EnvironmentRequest
from pants.engine.rules import Get, collect_rules, rule
from pants.option.option_types import StrListOption
from pants.option.subsystem import Subsystem
from pants.util.memo import memoized_method

DEFAULT_SEARCH_PATH = ["<GHCUP>", "<PATH>"]


class HaskellSubsystem(Subsystem):
    options_scope = "haskell"
    help = "Haskell"

    _search_paths = StrListOption(
        "--search-paths", default=DEFAULT_SEARCH_PATH, help="Search path for Haskell tools."
    )

    @memoized_method
    def search_paths(self, env: Environment) -> tuple[str, ...]:
        return tuple(_expand_search_paths(self._search_paths, env))


def _expand_search_paths(search_paths: Iterable[str], env: Environment) -> list[str]:
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


def get_environment_paths(env: Environment) -> list[str]:
    """Returns a list of paths specified by the PATH env var."""
    pathstr = env.get("PATH")
    if pathstr:
        return pathstr.split(os.pathsep)
    return []


def get_ghcup_paths(env: Environment) -> list[str]:
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


def get_ghcup_root(env: Environment) -> str | None:
    """See https://www.haskell.org/ghcup/install/#manual-install."""
    home_from_env = env.get("HOME")
    if home_from_env:
        return os.path.join(home_from_env, ".ghcup")
    return None


class GhcBinary(BinaryPath):
    """The Glasgow Haskell Compiler (GHC) tool."""


class CabalBinary(BinaryPath):
    """The Cabal build tool."""


@dataclass(frozen=True)
class HaskellToolRequest:
    name: str
    test_args: tuple[str, ...]
    rationale: str


@rule
async def find_haskell_tool(req: HaskellToolRequest, subsytem: HaskellSubsystem) -> BinaryPath:
    env = await Get(Environment, EnvironmentRequest(["HOME", "PATH"]))
    request = BinaryPathRequest(
        binary_name=req.name,
        search_path=SearchPath(subsytem.search_paths(env)),
        test=BinaryPathTest(args=req.test_args),
    )
    paths = await Get(BinaryPaths, BinaryPathRequest, request)
    return paths.first_path_or_raise(request, rationale=req.rationale)


@rule
async def find_ghc() -> GhcBinary:
    first_path = await Get(
        BinaryPath,
        HaskellToolRequest(
            name="ghc", test_args=("--version",), rationale="find a valid version for GHC"
        ),
    )
    return GhcBinary(first_path.path, first_path.fingerprint)


@rule
async def find_cabal() -> CabalBinary:
    first_path = await Get(
        BinaryPath,
        HaskellToolRequest(
            name="cabal", test_args=("--version",), rationale="find a valid version for Cabal"
        ),
    )
    return CabalBinary(first_path.path, first_path.fingerprint)


def rules():
    return collect_rules()
