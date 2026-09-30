"""Frozen D-034 model requests. These values never grant execution authority."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import InformationRef, ModelStateUpdateProposal, Revision, Subtask
from .serialization import freeze_json


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identifier must be a nonempty string")


class Action(StrEnum):
    NONE = "NONE"
    TOOL_REQUEST = "TOOL_REQUEST"
    WORK_BATCH = "WORK_BATCH"
    FINISH_PROPOSAL = "FINISH_PROPOSAL"


@dataclass(frozen=True)
class ToolCallRequest:
    request_id: str
    tool_name: str
    arguments: Mapping[str, Any]

    def __post_init__(self) -> None:
        identifier(self.request_id)
        identifier(self.tool_name)
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments must be a JSON object")
        object.__setattr__(self, "arguments", freeze_json(self.arguments))


@dataclass(frozen=True)
class FinishProposal:
    structured_output: Any
    information_refs: tuple[InformationRef, ...] = ()
    artifact_refs: tuple[InformationRef, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_output", freeze_json(self.structured_output))
        for name in ("information_refs", "artifact_refs"):
            refs = tuple(getattr(self, name))
            if any(not isinstance(ref, InformationRef) for ref in refs):
                raise ValueError("typed InformationRef required")
            object.__setattr__(self, name, refs)


@dataclass(frozen=True)
class WorkItem:
    work_id: str
    kind: str
    depends_on: tuple[str, ...]
    request_or_subtask: ToolCallRequest | Subtask
    budget_request: Mapping[str, float] = field(default_factory=dict)
    status: str = "PENDING"

    def __post_init__(self) -> None:
        identifier(self.work_id)
        expected = {"TOOL": ToolCallRequest, "SUBTASK": Subtask}.get(self.kind)
        if expected is None or not isinstance(self.request_or_subtask, expected):
            raise ValueError("WorkItem requires matching TOOL or SUBTASK payload")
        dependencies = tuple(self.depends_on)
        for dependency in dependencies:
            identifier(dependency)
        object.__setattr__(self, "depends_on", dependencies)
        object.__setattr__(self, "budget_request", freeze_json(self.budget_request))


@dataclass(frozen=True)
class WorkBatch:
    batch_id: str
    objective: str
    items: tuple[WorkItem, ...]
    budget_request: Mapping[str, float] = field(default_factory=dict)
    completion_policy: str = "ALL_SETTLED"

    def __post_init__(self) -> None:
        identifier(self.batch_id)
        object.__setattr__(self, "items", tuple(self.items))
        if any(not isinstance(item, WorkItem) for item in self.items):
            raise ValueError("typed WorkItem required")
        object.__setattr__(self, "budget_request", freeze_json(self.budget_request))
        # Policy and dependency validity are checked together before scheduling,
        # producing one structured rejection, rather than a partial execution.


@dataclass(frozen=True)
class ModelTurn:
    turn_id: str
    context_projection_ref: InformationRef
    base_revision: Revision
    action: Action
    state_update: ModelStateUpdateProposal | None = None
    tool_request: ToolCallRequest | None = None
    work_batch: WorkBatch | None = None
    finish_proposal: FinishProposal | None = None
    prose_summary: str | None = None

    def __post_init__(self) -> None:
        identifier(self.turn_id)
        if type(self.base_revision) not in (int, str):
            raise ValueError("base_revision must be an integer or string")
        object.__setattr__(self, "action", Action(self.action))
        variants = {Action.TOOL_REQUEST: (self.tool_request, ToolCallRequest),
                    Action.WORK_BATCH: (self.work_batch, WorkBatch),
                    Action.FINISH_PROPOSAL: (self.finish_proposal, FinishProposal)}
        for action, (value, expected) in variants.items():
            if action == self.action:
                if not isinstance(value, expected):
                    raise ValueError("action requires its typed payload")
            elif value is not None:
                raise ValueError("exactly one action variant may be active")
        if not isinstance(self.context_projection_ref, InformationRef):
            raise ValueError("typed projection reference required")
        if self.state_update is not None and not isinstance(
                self.state_update, ModelStateUpdateProposal):
            raise ValueError("typed state update required")
