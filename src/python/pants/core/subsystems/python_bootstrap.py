# Copyright 2020 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import annotations

import itertools
import logging
import os
import sys
from collections.abc import Collection
from dataclasses import dataclass

from pants.core.environments.target_types import EnvironmentTarget
from pants.core.util_rules import asdf, search_paths
from pants.core.util_rules.asdf import AsdfPathString, AsdfToolPathsResult
from pants.core.util_rules.search_paths import (
    ValidateSearchPathsRequest,
    VersionManagerSearchPathsRequest,
    get_un_cachable_version_manager_paths,
    validate_search_paths,
)
from pants.engine.env_vars import EnvironmentVars, EnvironmentVarsRequest, PathEnvironmentVariable
from pants.engine.internals.platform_rules import environment_vars_subset
from pants.engine.internals.selectors import concurrently
from pants.engine.rules import collect_rules, implicitly, rule
from pants.option.option_types import DictOption, StrListOption
from pants.option.subsystem import Subsystem
from pants.util.ordered_set import FrozenOrderedSet
from pants.util.strutil import help_text, softwrap

logger = logging.getLogger(__name__)

_PBS_URL_TEMPLATE = "https://github.com/astral-sh/python-build-standalone/releases/download/20241008/cpython-3.11.10+20241008-{}-install_only.tar.gz"


class PythonBootstrapSubsystem(Subsystem):
    options_scope = "python-bootstrap"
    help = help_text(
        """
        Options used to locate Python interpreters.

        This subsystem controls where and how Pants will locate Python, but beyond that it does
        not control which Python interpreter versions are actually used for your code: see the
        `python` subsystem for that.
        """
    )

    internal_python_build_standalone_info = DictOption(
        default={
            "linux_arm64": (
                _PBS_URL_TEMPLATE.format("aarch64-unknown-linux-gnu"),
                "320635e957e13d2e10d70a3031563d032fae9e40e60e5ec32bc353643fae1335",
                25925875,
            ),
            "linux_x86_64": (
                _PBS_URL_TEMPLATE.format("x86_64-unknown-linux-gnu"),
                "ff121f14ed113c9da83a45f76c3cf41976fb4419fe406d5cc7066765761c6a4e",
                29716764,
            ),
            "macos_arm64": (
                _PBS_URL_TEMPLATE.format("aarch64-apple-darwin"),
                "ecdc9c042b8f97bff211fcf9425bc51c96acd4037df1565964e89816f2c9564d",
                17795541,
            ),
            "macos_x86_64": (
                _PBS_URL_TEMPLATE.format("x86_64-apple-darwin"),
                "a618c086e0514f681523947e2b66a4dc0c6560f91c36faa072fa6787455df9ea",
                18165701,
            ),
        },
        help=softwrap(
            """
            A map from platform to the information needed to download Python Build Standalone.

            Python Build Standalone is used to run Python-implemented Pants tools/scripts in
            docker environments (so that Python doesn't need to be installed).

            The version of Python provided should match the default value's version, which is
            the highest Python Major/Minor version compatible with the Pants package's
            interpreter constraints. Additionally, the downloaded file should be extractable by
            `tar` using `-xvf` (most likely a `.tar.gz` file).

            The schema is `<string platform key>: (<string url>, <string fingerprint>, <int bytelen>)`
            for each possible platform.
            """
        ),
        advanced=True,
    )

    class EnvironmentAware(Subsystem.EnvironmentAware):
        search_path = StrListOption(
            default=["<PYENV>", AsdfPathString.STANDARD.value, "<PATH>"],
            help=softwrap(
                f"""
                A list of paths to search for Python interpreters.

                Which interpreters are actually used from these paths is context-specific:
                the Python backend selects interpreters using options on the `python` subsystem,
                in particular, the `[python].interpreter_constraints` option.

                You can specify absolute paths to interpreter binaries
                and/or to directories containing interpreter binaries. The order of entries does
                not matter.

                The following special strings are supported:

                For all runtime environment types:

                * `<PATH>`, the contents of the PATH env var

                When the environment is a `local_environment` target:

                * `{AsdfPathString.STANDARD}`, {AsdfPathString.STANDARD.description("Python")}
                * `{AsdfPathString.LOCAL}`, {AsdfPathString.LOCAL.description("interpreter")}
                * `<PYENV>`, all Python versions under `$(pyenv root)/versions`
                * `<PYENV_LOCAL>`, the Pyenv interpreter with the version in `BUILD_ROOT/.python-version`
                * `<PEXRC>`, paths in the `PEX_PYTHON_PATH` variable in `/etc/pexrc` or `~/.pexrc`
                """
            ),
            advanced=True,
            metavar="<binary-paths>",
        )
        names = StrListOption(
            default=["python", "python3"],
            help=softwrap(
                """
                The names of Python binaries to search for. See the `--search-path` option to
                influence where interpreters are searched for.

                This does not impact which Python interpreter is used to run your code, only what
                is used to run internal tools.
                """
            ),
            advanced=True,
            metavar="<python-binary-names>",
        )


@dataclass(frozen=True)
class PythonBootstrap:
    interpreter_names: tuple[str, ...]
    interpreter_search_paths: tuple[str, ...]


@dataclass(frozen=True)
class _ExpandInterpreterSearchPathsRequest:
    interpreter_search_paths: Collection[str]
    env_tgt: EnvironmentTarget


@dataclass(frozen=False)
class _SearchPaths:
    paths: tuple[str, ...]


@rule
async def _expand_interpreter_search_paths(
    request: _ExpandInterpreterSearchPathsRequest, path_env: PathEnvironmentVariable
) -> _SearchPaths:
    interpreter_search_paths, env_tgt = (request.interpreter_search_paths, request.env_tgt)

    asdf_paths = await AsdfToolPathsResult.get_un_cachable_search_paths(
        interpreter_search_paths,
        env_tgt=env_tgt,
        tool_name="python",
        tool_description="Python interpreters",
        paths_option_name="[python-bootstrap].search_path",
    )

    asdf_standard_tool_paths, asdf_local_tool_paths = (
        asdf_paths.standard_tool_paths,
        asdf_paths.local_tool_paths,
    )

    special_strings = {
        "<PEXRC>": _get_pex_python_paths,
        "<PATH>": lambda: path_env,
        AsdfPathString.STANDARD: lambda: asdf_standard_tool_paths,
        AsdfPathString.LOCAL: lambda: asdf_local_tool_paths,
    }

    expanded: list[str] = []
    from_pexrc = None

    pyenv_env = await environment_vars_subset(
        EnvironmentVarsRequest(("PYENV_ROOT", "HOME")), **implicitly()
    )
    pyenv_root = _get_pyenv_root(pyenv_env)
    pyenv_path_results = await concurrently(
        get_un_cachable_version_manager_paths(
            VersionManagerSearchPathsRequest(
                env_tgt,
                pyenv_root,
                "versions",
                f"[{PythonBootstrapSubsystem.options_scope}].search_path",
                (".python-version",),
                s if s == "<PYENV_LOCAL>" else None,
            )
        )
        for s in interpreter_search_paths
        if s == "<PYENV>" or s == "<PYENV_LOCAL>"
    )
    for pyenv_path in FrozenOrderedSet(itertools.chain.from_iterable(pyenv_path_results)):
        expanded.append(pyenv_path)
    for s in interpreter_search_paths:
        if s in special_strings:
            special_paths = special_strings[s]()
            if s == "<PEXRC>":
                from_pexrc = special_paths
            expanded.extend(special_paths)
        elif s == "<PYENV>" or s == "<PYENV_LOCAL>":
            continue
        else:
            expanded.append(s)
    # Some special-case logging to avoid misunderstandings.
    if from_pexrc and len(expanded) > len(from_pexrc):
        logger.info(
            softwrap(
                f"""
                pexrc interpreters requested and found, but other paths were also specified,
                so interpreters may not be restricted to the pexrc ones. Full search path is:

                {":".join(expanded)}
                """
            )
        )
    return _SearchPaths(tuple(expanded))


# This method is copied from the pex package, located at pex.variables.Variables._get_kv().
# It is copied here to avoid a hard dependency on pex.
def _get_kv(variable: str) -> list[str] | None:
    kv = variable.strip().split("=")
    if len(list(filter(None, kv))) == 2:
        return kv
    else:
        return None


# This method is copied from the pex package, located at pex.variables.Variables.from_rc().
# It is copied here to avoid a hard dependency on pex.
def _read_pex_rc(rc: str | None = None) -> dict[str, str]:
    """Read pex runtime configuration variables from a pexrc file.

    :param rc: an absolute path to a pexrc file.
    :return: A dict of key value pairs found in processed pexrc files.
    """
    ret_vars = {}
    rc_locations = [
        os.path.join(os.sep, "etc", "pexrc"),
        os.path.join("~", ".pexrc"),
        os.path.join(os.path.dirname(sys.argv[0]), ".pexrc"),
    ]
    if rc:
        rc_locations.append(rc)
    for filename in rc_locations:
        try:
            with open(os.path.expanduser(filename)) as fh:
                rc_items = map(_get_kv, fh)
                ret_vars.update(dict(filter(None, rc_items)))
        except OSError:
            continue
    return ret_vars


def _get_pex_python_paths():
    """Returns a list of paths to Python interpreters as defined in a pexrc file.

    These are provided by a PEX_PYTHON_PATH in either of '/etc/pexrc', '~/.pexrc'. PEX_PYTHON_PATH
    defines a colon-separated list of paths to interpreters that a pex can be built and run against.
    """
    ppp = _read_pex_rc().get("PEX_PYTHON_PATH")
    if ppp:
        return ppp.split(os.pathsep)
    else:
        return []


def _get_pyenv_root(env: EnvironmentVars) -> str | None:
    """See https://github.com/pyenv/pyenv#environment-variables."""
    from_env = env.get("PYENV_ROOT")
    if from_env:
        return from_env
    home_from_env = env.get("HOME")
    if home_from_env:
        return os.path.join(home_from_env, ".pyenv")
    return None


@rule
async def python_bootstrap(
    python_bootstrap_subsystem: PythonBootstrapSubsystem.EnvironmentAware,
) -> PythonBootstrap:
    interpreter_search_paths = await validate_search_paths(
        ValidateSearchPathsRequest(
            env_tgt=python_bootstrap_subsystem.env_tgt,
            search_paths=tuple(python_bootstrap_subsystem.search_path),
            option_origin=f"[{PythonBootstrapSubsystem.options_scope}].search_path",
            environment_key="python_bootstrap_search_path",
            is_default=python_bootstrap_subsystem._is_default("search_path"),
            local_only=FrozenOrderedSet(
                (
                    "<PYENV>",
                    "<PYENV_LOCAL>",
                    AsdfPathString.STANDARD,
                    AsdfPathString.LOCAL,
                    "<PEXRC>",
                )
            ),
        )
    )
    interpreter_names = python_bootstrap_subsystem.names

    expanded_paths = await _expand_interpreter_search_paths(
        _ExpandInterpreterSearchPathsRequest(
            interpreter_search_paths,
            python_bootstrap_subsystem.env_tgt,
        ),
        **implicitly(),
    )

    return PythonBootstrap(
        interpreter_names=interpreter_names,
        interpreter_search_paths=expanded_paths.paths,
    )


def rules():
    return (
        *collect_rules(),
        *asdf.rules(),
        *search_paths.rules(),
    )
