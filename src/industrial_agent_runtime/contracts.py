"""Generic contracts owned by docs/specs/runtime-v0.md.

Consumer content stays opaque. No domain state or provider SDK types are imported.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, TypeAlias

from .serialization import freeze_json

Revision: TypeAlias = int | str


class Visibility(StrEnum):
    AGENT = "AGENT"
    EVALUATOR = "EVALUATOR"
    INTERNAL = "INTERNAL"


class TaskStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    READY = "READY"
    DONE = "DONE"
    FAILED = "FAILED"
    EXHAUSTED = "EXHAUSTED"
    CANCELLED = "CANCELLED"


class SideEffectClass(StrEnum):
    READ = "READ"
    COMPUTE = "COMPUTE"
    SIMULATE = "SIMULATE"
    PROPOSE = "PROPOSE"
    MUTATE = "MUTATE"
    ADMIN = "ADMIN"


@dataclass(frozen=True)
class InformationRef:
    ref_id: str
    kind: str
    owner: str
    version: str
    visibility: Visibility
    created_at: str
    checksum: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "visibility", Visibility(self.visibility))
        for name in ("ref_id", "kind", "owner", "version", "created_at"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a nonempty string")


@dataclass(frozen=True)
class Budget:
    max_model_calls: int
    max_tool_calls: int
    max_subagents: int
    max_subagent_depth: int
    max_steps: int
    max_total_tokens: int | None = None
    max_parallel_width: int | None = None
    extra_dimensions: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("max_model_calls", "max_tool_calls", "max_subagents",
                     "max_subagent_depth", "max_steps", "max_total_tokens",
                     "max_parallel_width"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer")
        if self.max_parallel_width == 0:
            raise ValueError("max_parallel_width must be positive when configured")
        draw = freeze_json(self.extra_dimensions)
        for key, value in draw.items():
            if not key or type(value) not in (int, float) or value < 0:
                raise ValueError("extra_dimensions must contain nonnegative numbers")
        object.__setattr__(self, "extra_dimensions", draw)


@dataclass(frozen=True)
class Task:
    task_id: str
    goal: str
    context_refs: tuple[InformationRef, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    output_schema: Mapping[str, Any]
    parent_task_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_refs", tuple(self.context_refs))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "output_schema", freeze_json(self.output_schema))
        object.__setattr__(self, "metadata", freeze_json(self.metadata))


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    output_schema: Mapping[str, Any]
    side_effect_class: SideEffectClass
    required_policy_tags: tuple[str, ...] = ()
    declared_budget_draw: Mapping[str, float | str] = field(default_factory=dict)
    max_budget_draw: Mapping[str, float] = field(default_factory=dict)
    isolation_guarantee: str | None = None
    provider_metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "side_effect_class", SideEffectClass(self.side_effect_class))
        object.__setattr__(self, "required_policy_tags", tuple(self.required_policy_tags))
        for name in ("input_schema", "output_schema", "declared_budget_draw",
                     "max_budget_draw", "provider_metadata"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))


@dataclass(frozen=True)
class ToolResult:
    request_id: str
    status: str
    structured_output: Any
    actual_budget_draw: Mapping[str, float]
    provenance: Mapping[str, Any]
    artifact_refs: tuple[InformationRef, ...] = ()
    information_refs: tuple[InformationRef, ...] = ()
    error: str | None = None

    def __post_init__(self) -> None:
        for name in ("structured_output", "actual_budget_draw", "provenance"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        object.__setattr__(self, "information_refs", tuple(self.information_refs))


@dataclass(frozen=True)
class StateDelta:
    operation: str
    target_ref_or_path: str
    value_or_ref: Any
    producer: str
    proposed_base_revision: Revision | None = None
    reason_ref: InformationRef | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "value_or_ref", freeze_json(self.value_or_ref))


@dataclass(frozen=True)
class ModelStateUpdateProposal:
    proposal_id: str
    base_revision: Revision
    deltas: tuple[StateDelta, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "deltas", tuple(self.deltas))


@dataclass(frozen=True)
class ContextProjection:
    projection_id: str
    task_id: str
    base_revision: Revision
    content: Any
    included_refs: tuple[InformationRef, ...]
    visibility_policy_version: str
    approximate_tokens: int
    checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", freeze_json(self.content))
        object.__setattr__(self, "included_refs", tuple(self.included_refs))
        if any(ref.visibility == Visibility.EVALUATOR for ref in self.included_refs):
            raise ValueError("EVALUATOR references cannot enter a ContextProjection")
        if type(self.approximate_tokens) is not int or self.approximate_tokens < 0:
            raise ValueError("approximate_tokens must be a nonnegative integer")


@dataclass(frozen=True)
class RuntimeResult:
    task_id: str
    status: TaskStatus
    structured_output: Any
    state_revision: Revision
    trace_ref: InformationRef
    budget_usage: Mapping[str, float]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", TaskStatus(self.status))
        object.__setattr__(self, "structured_output", freeze_json(self.structured_output))
        object.__setattr__(self, "budget_usage", freeze_json(self.budget_usage))
        object.__setattr__(self, "warnings", tuple(self.warnings))
        object.__setattr__(self, "errors", tuple(self.errors))


@dataclass(frozen=True)
class Subtask:
    subtask_id: str
    parent_task_id: str
    goal: str
    context_refs: tuple[InformationRef, ...]
    allowed_tools: tuple[str, ...]
    budget: Budget
    output_schema: Mapping[str, Any]
    reason_for_delegation: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_refs", tuple(self.context_refs))
        object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        object.__setattr__(self, "output_schema", freeze_json(self.output_schema))


@dataclass(frozen=True)
class SubtaskResult:
    subtask_id: str
    status: str
    claim_or_result: Any
    observation_refs: tuple[InformationRef, ...]
    artifact_refs: tuple[InformationRef, ...]
    warnings: tuple[str, ...]
    budget_usage: Mapping[str, float]
    trace_ref: InformationRef
    uncertainty_or_confidence: Any = None

    def __post_init__(self) -> None:
        for name in ("claim_or_result", "budget_usage", "uncertainty_or_confidence"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        for name in ("observation_refs", "artifact_refs", "warnings"):
            object.__setattr__(self, name, tuple(getattr(self, name)))


@dataclass(frozen=True)
class TraceEvent:
    event_id: str
    task_id: str
    type: str
    timestamp: str
    status: str
    budget_delta: Mapping[str, float]
    input_summary: Any
    output_summary: Any
    parent_task_id: str | None = None
    batch_id: str | None = None
    work_id: str | None = None
    subtask_id: str | None = None
    request_id: str | None = None
    error: str | None = None
    latency: float | None = None
    cost: float | None = None
    artifact_refs: tuple[InformationRef, ...] = ()
    context_projection_ref: InformationRef | None = None
    prompt_template_version: str | None = None
    provider: str | None = None
    model: str | None = None
    model_version: str | None = None
    sampling_parameters: Mapping[str, Any] = field(default_factory=dict)
    registered_tool_set_version: str | None = None

    def __post_init__(self) -> None:
        for name in ("budget_delta", "input_summary", "output_summary",
                     "sampling_parameters"):
            object.__setattr__(self, name, freeze_json(getattr(self, name)))
        object.__setattr__(self, "artifact_refs", tuple(self.artifact_refs))
        if self.type == "MODEL_TURN":
            for name in ("context_projection_ref", "prompt_template_version",
                         "provider", "model", "model_version",
                         "registered_tool_set_version"):
                if not getattr(self, name):
                    raise ValueError(f"MODEL_TURN requires {name}")
