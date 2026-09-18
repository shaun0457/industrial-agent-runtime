"""Explicit trusted integration seams for B2/B3 and adapter execution.

Hooks are application code, never model-authored callbacks. No hook means denial.
The B1 coordinator supports only READ/COMPUTE with no extra resource draw; B2/B3
must implement full schema/policy/reservation and verification before extending it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .actions import FinishProposal, ModelTurn, ToolCallRequest
from .contracts import (ContextProjection, InformationRef, Revision, StateDelta,
                        Task, ToolResult, ToolSpec)
from .serialization import freeze_json


@dataclass(frozen=True)
class GateDecision:
    request_id: str
    decision: str
    stage: str
    reason_code: str
    reason: str
    policy_version: str
    validator_refs: tuple[InformationRef, ...] = ()
    reserved_budget_draw: Mapping[str, float] = field(default_factory=dict)
    expected_state_revision: Revision | None = None
    normalized_request_ref: InformationRef | None = None

    def __post_init__(self) -> None:
        if self.decision not in {"ALLOW", "DENY", "REQUIRE_APPROVAL"}:
            raise ValueError("unknown gate decision")
        object.__setattr__(self, "reserved_budget_draw", freeze_json(self.reserved_budget_draw))
        object.__setattr__(self, "validator_refs", tuple(self.validator_refs))


class ModelProvider(Protocol):
    def generate(self, context_projection: ContextProjection,
                 tool_specs: Sequence[ToolSpec], output_schema: Mapping[str, Any],
                 limits: Mapping[str, Any]) -> ModelTurn: ...


class RequestGate(Protocol):
    def validate_request(self, request: ToolCallRequest, spec: ToolSpec, task: Task,
                         expected_state_revision: Revision,
                         budget_usage: Mapping[str, int]) -> GateDecision:
        """Validate schema, policy, and consumer conditions for the exact request."""
        ...


class Executor(Protocol):
    def execute(self, request: ToolCallRequest, spec: ToolSpec) -> ToolResult:
        """Execute the frozen request with adapter-enforced resource/time limits."""
        ...


class ResultVerifier(Protocol):
    def verify_result(self, result: ToolResult, request: ToolCallRequest,
                      spec: ToolSpec, deltas: Sequence[StateDelta],
                      expected_state_revision: Revision) -> bool:
        """True only after schema, refs, provenance, and ingestion invariants pass."""
        ...

    def verify_finish(self, proposal: FinishProposal, task: Task,
                      expected_state_revision: Revision) -> bool:
        """True only after output schema, every ref, and readiness checks pass."""
        ...


class ResultIngestor(Protocol):
    def derive_deltas(self, result: ToolResult) -> Sequence[StateDelta]:
        """Pure deterministic consumer mapping; must not mutate task state."""
        ...
