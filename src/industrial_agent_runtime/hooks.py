"""Explicit trusted integration seams for B2/B3 and adapter execution.

Hooks are application code, never model-authored callbacks. No hook means denial.
The B1 coordinator supports only READ/COMPUTE with no extra resource draw; B2/B3
must implement full schema/policy/reservation and verification before extending it.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math
import re
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


_ERROR_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")

# Normalised finish reasons; an adapter maps anything else to "other", so provider
# text never reaches the trace through this field.
FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "other"})


def is_error_code(value: Any) -> bool:
    """True for a provider error code: [A-Z][A-Z0-9_]*, at most 64 characters."""
    return isinstance(value, str) and _ERROR_CODE.fullmatch(value) is not None


@dataclass(frozen=True)
class ModelUsage:
    """What one generate() call cost, as reported by the provider adapter.

    attempts counts every provider request, retries included, with one error code
    in retry_reasons per retried attempt. Token counts are None when the provider
    did not report them; None is never read as zero.
    """
    attempts: int
    retry_reasons: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    latency_seconds: float = 0.0

    def __post_init__(self) -> None:
        if type(self.attempts) is not int or self.attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if isinstance(self.retry_reasons, (str, bytes)):
            raise ValueError("retry_reasons must be a sequence of codes, not one string")
        reasons = tuple(self.retry_reasons)
        if not all(is_error_code(reason) for reason in reasons):
            raise ValueError("retry_reasons must be error codes [A-Z][A-Z0-9_]*")
        if len(reasons) != self.attempts - 1:
            raise ValueError("retry_reasons must hold one code per retried attempt")
        object.__setattr__(self, "retry_reasons", reasons)
        for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        if self.finish_reason is not None and self.finish_reason not in FINISH_REASONS:
            raise ValueError(f"finish_reason must be one of {sorted(FINISH_REASONS)} or None")
        latency = self.latency_seconds
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("latency_seconds must be a finite nonnegative number")


class ProviderError(RuntimeError):
    """Base for provider failures. The message is the code alone, never upstream
    text, so traces and RuntimeResult.errors cannot carry keys or user content.

    usage reports the attempts made before the failure (e.g. retried timeouts), so
    the Coordinator can charge and trace them. The Coordinator re-checks the code
    and never reads str(exc), so a subclass cannot smuggle text through either.
    """

    def __init__(self, code: str, *, usage: ModelUsage | None = None) -> None:
        if not is_error_code(code):
            raise ValueError("provider error code must match [A-Z][A-Z0-9_]*")
        if usage is not None and not isinstance(usage, ModelUsage):
            raise ValueError("usage must be a ModelUsage or None")
        super().__init__(code)
        self._code = code
        self._usage = usage

    @property
    def code(self) -> str:
        return self._code

    @property
    def usage(self) -> ModelUsage | None:
        return self._usage


class ProviderOutputError(ProviderError):
    """A reply arrived but is not a valid turn: the Coordinator rejects the turn.

    Adapters do not retry output errors; usage covers transport retries that
    happened before the malformed reply arrived.
    """


class ProviderTransportError(ProviderError):
    """No usable reply after the adapter's retry policy: the run fails."""


@dataclass(frozen=True)
class ProviderResponse:
    """A typed turn plus the usage of the provider call that produced it."""
    turn: ModelTurn
    usage: ModelUsage

    def __post_init__(self) -> None:
        if not isinstance(self.turn, ModelTurn) or not isinstance(self.usage, ModelUsage):
            raise ValueError("ProviderResponse requires a ModelTurn and ModelUsage")


class ModelProvider(Protocol):
    def generate(self, context_projection: ContextProjection,
                 tool_specs: Sequence[ToolSpec], output_schema: Mapping[str, Any],
                 limits: Mapping[str, Any]) -> ModelTurn | ProviderResponse:
        """Return a typed turn, or a ProviderResponse when usage is known.

        Raise ProviderOutputError when a reply arrived but is not a valid turn and
        ProviderTransportError when no usable reply arrived.
        """
        ...


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
