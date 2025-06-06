# Copyright 2023 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).
import json
import logging
from typing import Any

from pants.engine.internals.scheduler import Workunit
from pants.engine.rules import collect_rules, rule
from pants.engine.streaming_workunit_handler import (
    StreamingWorkunitContext,
    WorkunitsCallback,
    WorkunitsCallbackFactory,
    WorkunitsCallbackFactoryRequest,
)
from pants.engine.unions import UnionRule
from pants.option.option_types import BoolOption, StrOption
from pants.option.subsystem import Subsystem
from pants.util.dirutil import safe_open

logger = logging.getLogger(__name__)


def just_dump_map(workunits_map):
    return [
        {
            k: v
            for k, v in wu.items()
            if k
            in (
                "name",
                "span_id",
                "level",
                "parent_id",
                "start_secs",
                "start_nanos",
                "description",
                "duration_secs",
                "duration_nanos",
                "metadata",
            )
        }
        for wu in workunits_map.values()
    ]


def pass_through_unserializable_metadata(obj):
    """Most of the metadata in workunit objects is json serializable, but it is not guaranteed that
    all of it is.

    This function is intended to be used as a `default` encoder to json.dumps to drop a minimal stub
    encoding with the name instead of throwing a TypeError and halting the entire workunit logging.
    """
    return {"name": obj.__class__.__name__, "json_serializable": False, "str": str(obj)}


class WorkunitLoggerCallback(WorkunitsCallback):
    """Configuration for WorkunitLogger."""

    def __init__(self, wulogger: "WorkunitLogger"):
        self.wulogger = wulogger
        self._completed_workunits: dict[str, object] = {}

    @property
    def can_finish_async(self) -> bool:
        return False

    def __call__(
        self,
        *,
        completed_workunits: tuple[Workunit, ...],
        started_workunits: tuple[Workunit, ...],
        context: StreamingWorkunitContext,
        finished: bool = False,
        **kwargs: Any,
    ) -> None:
        for wu in completed_workunits:
            self._completed_workunits[wu["span_id"]] = wu
        if finished:
            filepath = f"{self.wulogger.logdir}/{context.run_tracker.run_id}.json"
            with safe_open(filepath, "w") as f:
                json.dump(
                    just_dump_map(self._completed_workunits),
                    f,
                    default=pass_through_unserializable_metadata,
                )
                logger.info(f"Wrote log to {filepath}")


class WorkunitLoggerCallbackFactoryRequest:
    """A unique request type that is installed to trigger construction of our WorkunitsCallback."""


class WorkunitLogger(Subsystem):
    options_scope = "workunit-logger"
    help = "Workunit Logger subsystem. Useful for debugging pants itself."

    enabled = BoolOption("--enabled", default=False, help="Whether to enable workunit logging.")
    logdir = StrOption("--logdir", default=".pants.d", help="Where to write the log to.")


@rule
def construct_callback(
    _: WorkunitLoggerCallbackFactoryRequest,
    wulogger: WorkunitLogger,
) -> WorkunitsCallbackFactory:
    return WorkunitsCallbackFactory(
        lambda: WorkunitLoggerCallback(wulogger) if wulogger.enabled else None
    )


def rules():
    return [
        UnionRule(WorkunitsCallbackFactoryRequest, WorkunitLoggerCallbackFactoryRequest),
        *collect_rules(),
    ]
