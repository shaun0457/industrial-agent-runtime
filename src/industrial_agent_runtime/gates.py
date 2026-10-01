"""B2 deterministic pre-execution gates (docs/specs/deterministic-gates-v0.md).

G0 schema -> G1 allowlist/authority -> G2 budget/reservation (static sizing, trusted
request-bound resolution, quota) -> G3 side-effect -> consumer validate_request ->
optional approval -> frozen request.

Every stage yields a GateDecision. Any denial, unknown policy, or gate error
prevents dispatch. The runtime stays domain-free: which extra budget dimensions
represent simulation work is consumer configuration, not runtime knowledge.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
import math
from typing import Any, Protocol

from .actions import ToolCallRequest
from .contracts import (Budget, InformationRef, Revision, SideEffectClass, Task,
                        ToolSpec, Visibility)
from .hooks import GateDecision, RequestGate
from .schema import instance_errors, schema_errors
from .serialization import checksum, freeze_json

STANDARD_DIMENSIONS = frozenset({"model_calls", "tool_calls", "subagents", "steps"})
_REF_FIELDS = frozenset({"ref_id", "kind", "owner", "version", "visibility", "created_at",
                         "checksum"})
_NON_ISOLATING = frozenset({"", "NONE"})


class GateStage(StrEnum):
    G0_SCHEMA = "G0_SCHEMA"
    G1_AUTHORITY = "G1_AUTHORITY"
    G2_BUDGET = "G2_BUDGET"
    G3_SIDE_EFFECT = "G3_SIDE_EFFECT"
    CONSUMER = "CONSUMER"
    APPROVAL = "APPROVAL"
    DISPATCH = "DISPATCH"


class GateDenied(ValueError):
    """Structured deterministic denial; carries the GateDecision to trace."""

    def __init__(self, decision: GateDecision) -> None:
        super().__init__(f"{decision.stage}/{decision.reason_code}: {decision.reason}")
        self.decision = decision


class ReferenceStateGuard(Protocol):
    def reference_revision(self) -> Revision:
        """Current revision/version of the consumer's reference (non-sandbox) state."""
        ...


class ReservationResolver(Protocol):
    def resolve_reservation(self, request: ToolCallRequest,
                            spec: ToolSpec) -> Mapping[str, float]:
        """Exact draw for some dynamic (string-declared) dimensions of this request.

        Trusted application code, never model-authored. Must be a pure function of
        the validated request and ToolSpec: no tool execution, state mutation,
        authority grant, or expression evaluation. Omitted dimensions reserve the max.
        """
        ...


class ReservationOrigin(StrEnum):
    DECLARED = "DECLARED"
    REQUEST_BOUND = "REQUEST_BOUND"
    MAX_FALLBACK = "MAX_FALLBACK"


@dataclass(frozen=True)
class ResolvedReservation:
    """Exact per-dimension reservation and where each amount came from."""

    draw: Mapping[str, float]
    origins: Mapping[str, str]  # dimension -> ReservationOrigin value

    def __post_init__(self) -> None:
        object.__setattr__(self, "draw", freeze_json(self.draw))
        object.__setattr__(self, "origins", freeze_json(self.origins))


class ApprovalHook(Protocol):
    def decide(self, frozen: "FrozenRequest") -> str:
        """Return APPROVE or DENY for the exact frozen request. Never edits it."""
        ...


@dataclass(frozen=True)
class GatePolicy:
    """Trusted application-supplied execution authority. Never model-authored.

    Separate from ``Task`` (requested work/tool surface); effective authority is the
    intersection of Task permissions and these grants. Child policies are created
    only through ``delegate``: tools/classes/tags may only narrow, approval may only
    become stricter, and ``simulation_dimensions`` are inherited unchanged.
    """

    policy_version: str
    granted_side_effect_classes: frozenset[SideEffectClass] = frozenset(
        {SideEffectClass.READ, SideEffectClass.COMPUTE})
    granted_policy_tags: frozenset[str] = frozenset()
    simulation_dimensions: frozenset[str] = frozenset()
    approval_required_for: frozenset[SideEffectClass] = frozenset({SideEffectClass.MUTATE})
    tool_allowlist: frozenset[str] | None = None
    parent: "GatePolicy | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy_version, str) or not self.policy_version:
            raise ValueError("policy_version must be a nonempty string")
        classes = frozenset(SideEffectClass(item) for item in self.granted_side_effect_classes)
        if SideEffectClass.ADMIN in classes:
            raise ValueError("ADMIN authority cannot be granted in v0")
        approval = frozenset(SideEffectClass(item) for item in self.approval_required_for)
        if SideEffectClass.MUTATE not in approval:
            raise ValueError("MUTATE always requires approval in v0")
        object.__setattr__(self, "granted_side_effect_classes", classes)
        object.__setattr__(self, "approval_required_for", approval)
        for name in ("granted_policy_tags", "simulation_dimensions"):
            values = frozenset(getattr(self, name))
            if any(not isinstance(item, str) or not item for item in values):
                raise ValueError(f"{name} must contain nonempty strings")
            object.__setattr__(self, name, values)
        if self.simulation_dimensions & STANDARD_DIMENSIONS:
            raise ValueError("simulation dimensions must be extra dimensions")
        if self.tool_allowlist is not None:
            object.__setattr__(self, "tool_allowlist", frozenset(self.tool_allowlist))
        parent = self.parent
        if parent is not None:
            if self.tool_allowlist is None or parent.tool_allowlist is None:
                raise ValueError("delegation requires explicit parent and child tool allowlists")
            if (not classes <= parent.granted_side_effect_classes
                    or not self.granted_policy_tags <= parent.granted_policy_tags
                    or not self.tool_allowlist <= parent.tool_allowlist
                    or not parent.approval_required_for <= approval):
                raise ValueError("child authority must be a subset of parent authority")
            # Classification semantics, not authority: inherited unchanged in v0.
            if self.simulation_dimensions != parent.simulation_dimensions:
                raise ValueError("child policy must inherit simulation_dimensions unchanged")

    def delegate(self, policy_version: str, *, tools: frozenset[str],
                 classes: frozenset[SideEffectClass] | None = None,
                 tags: frozenset[str] = frozenset()) -> "GatePolicy":
        return GatePolicy(policy_version,
                          self.granted_side_effect_classes if classes is None else classes,
                          tags, self.simulation_dimensions, self.approval_required_for,
                          frozenset(tools), self)

    def allows_tool(self, name: str) -> bool:
        policy: GatePolicy | None = self
        while policy is not None:
            if policy.tool_allowlist is not None and name not in policy.tool_allowlist:
                return False
            policy = policy.parent
        return True


DEFAULT_POLICY = GatePolicy("runtime-default-v0")


@dataclass(frozen=True)
class FrozenRequest:
    """Exact authorized request plus every binding checked again before dispatch."""

    request: ToolCallRequest
    spec_checksum: str
    side_effect_class: SideEffectClass
    reserved_budget_draw: Mapping[str, float]
    expected_state_revision: Revision
    expected_reference_revision: Revision | None
    policy_version: str
    decisions: tuple[GateDecision, ...] = field(default=())
    reservation_origins: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "reserved_budget_draw", freeze_json(self.reserved_budget_draw))
        object.__setattr__(self, "decisions", tuple(self.decisions))
        object.__setattr__(self, "reservation_origins", freeze_json(self.reservation_origins))


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def ref_envelopes(value: Any) -> list[Mapping[str, Any]]:
    """Every mapping that claims to be an InformationRef (has ``ref_id``)."""
    if isinstance(value, Mapping):
        found = [value] if "ref_id" in value else []
        return found + [ref for item in value.values() for ref in ref_envelopes(item)]
    if isinstance(value, (tuple, list)):
        return [ref for item in value for ref in ref_envelopes(item)]
    return []


class GatePipeline:
    """Deterministic, stateless evaluation of one request against one run state."""

    def __init__(self, task: Task, specs: Mapping[str, ToolSpec], policy: GatePolicy,
                 consumer: RequestGate | None, approval: ApprovalHook | None,
                 reference_guard: ReferenceStateGuard | None, *,
                 resolver: ReservationResolver | None = None) -> None:
        collisions = set(task.budget.extra_dimensions) & STANDARD_DIMENSIONS
        if collisions:
            raise ValueError(f"extra dimensions shadow standard counters: {sorted(collisions)}")
        self.task, self.specs, self.policy = task, specs, policy
        self.consumer, self.approval, self.reference_guard = consumer, approval, reference_guard
        self.resolver = resolver

    # -- decisions ---------------------------------------------------------------------
    def _decision(self, request_id: str, stage: GateStage, reason_code: str, reason: str,
                  decision: str = "DENY", **fields: Any) -> GateDecision:
        return GateDecision(request_id, decision, stage.value, reason_code, reason,
                            self.policy.policy_version, **fields)

    def _deny(self, request_id: str, stage: GateStage, reason_code: str, reason: str):
        raise GateDenied(self._decision(request_id, stage, reason_code, reason))

    # -- static stages (also used by WorkBatch preflight) ---------------------------------
    def static_check(self, request: Any, known_refs: Mapping[str, InformationRef],
                     ) -> tuple[ToolSpec, ResolvedReservation, list[GateDecision]]:
        """G0, G1, G2 reservation sizing (static + request-bound), and G3.

        No state or budget change; quota is checked separately by ``check_budget``.
        """
        request_id = getattr(request, "request_id", "<untyped>")
        request_id = request_id if isinstance(request_id, str) else "<untyped>"
        if not isinstance(request, ToolCallRequest):
            self._deny(request_id, GateStage.G0_SCHEMA, "UNTYPED_REQUEST",
                       "typed ToolCallRequest required")
        spec = self.specs.get(request.tool_name)
        if spec is None:
            self._deny(request_id, GateStage.G0_SCHEMA, "UNKNOWN_TOOL",
                       "tool_name does not identify a registered ToolSpec")
        problems = schema_errors(spec.input_schema) + schema_errors(spec.output_schema)
        if problems:
            self._deny(request_id, GateStage.G0_SCHEMA, "UNSUPPORTED_SCHEMA", "; ".join(problems))
        problems = instance_errors(request.arguments, spec.input_schema)
        if problems:
            self._deny(request_id, GateStage.G0_SCHEMA, "INVALID_ARGUMENTS", "; ".join(problems))
        self._check_refs(request_id, request.arguments, known_refs)
        decisions = [self._decision(request_id, GateStage.G0_SCHEMA, "SCHEMA_VALID",
                                    "request parsed and matched input schema", "ALLOW")]

        cls = spec.side_effect_class
        if (request.tool_name not in self.task.allowed_tools
                or not self.policy.allows_tool(request.tool_name)):
            self._deny(request_id, GateStage.G1_AUTHORITY, "TOOL_NOT_ALLOWLISTED",
                       "tool is not explicitly allowlisted for this task/policy")
        if self.task.parent_task_id is not None and self.policy.parent is None:
            self._deny(request_id, GateStage.G1_AUTHORITY, "CHILD_AUTHORITY_UNDELEGATED",
                       "child task requires an explicitly delegated policy")
        if cls not in self.policy.granted_side_effect_classes:
            self._deny(request_id, GateStage.G1_AUTHORITY, "CLASS_NOT_GRANTED",
                       f"{cls.value} authority is not granted")
        if not set(spec.required_policy_tags) <= self.policy.granted_policy_tags:
            self._deny(request_id, GateStage.G1_AUTHORITY, "POLICY_TAG_NOT_GRANTED",
                       "required policy tags are not granted")
        decisions.append(self._decision(request_id, GateStage.G1_AUTHORITY, "AUTHORIZED",
                                        "tool, class, and tags explicitly granted", "ALLOW"))

        reservation = self.resolve(request, spec)
        decisions.append(self._side_effect(request_id, spec, reservation.draw))
        return spec, reservation, decisions

    def _check_refs(self, request_id, arguments, known_refs):
        for envelope in ref_envelopes(arguments):
            try:
                if not set(envelope) <= _REF_FIELDS:
                    raise ValueError("unknown ref fields")
                ref = InformationRef(**envelope)
            except (TypeError, ValueError):
                self._deny(request_id, GateStage.G0_SCHEMA, "MALFORMED_REF",
                           "reference envelope is not a valid InformationRef")
            if ref.visibility != Visibility.AGENT:
                self._deny(request_id, GateStage.G0_SCHEMA, "HIDDEN_REF",
                           "executable request contains a non-AGENT reference")
            if known_refs.get(ref.ref_id) != ref:
                self._deny(request_id, GateStage.G0_SCHEMA, "UNKNOWN_REF",
                           "reference is not a known Agent-visible ref of this run")

    def reservation(self, request_id: str, spec: ToolSpec) -> dict[str, float]:
        """Per-call reservation: numeric declared draw, else the declared maximum.

        String (expression) draws are not interpreted by the v0 runtime; they reserve
        ``max_budget_draw`` and are unreservable without one.
        """
        declared, maximum = spec.declared_budget_draw, spec.max_budget_draw
        reserved: dict[str, float] = {}
        for name in sorted(set(declared) | set(maximum)):
            if name in STANDARD_DIMENSIONS:
                self._deny(request_id, GateStage.G0_SCHEMA, "INVALID_TOOL_SPEC",
                           f"{name} is runtime-charged and cannot be declared by a tool")
            cap = maximum.get(name)
            if cap is not None and not _finite(cap):
                self._deny(request_id, GateStage.G0_SCHEMA, "INVALID_TOOL_SPEC",
                           f"max_budget_draw[{name}] must be a finite nonnegative number")
            value = declared.get(name)
            if isinstance(value, str) or value is None:
                if cap is None:
                    self._deny(request_id, GateStage.G2_BUDGET, "UNRESERVABLE_DRAW",
                               f"{name} has no numeric declared draw or maximum")
                amount = cap
            elif _finite(value) and type(value) is not bool:
                if cap is not None and value > cap:
                    self._deny(request_id, GateStage.G0_SCHEMA, "INVALID_TOOL_SPEC",
                               f"declared draw for {name} exceeds its maximum")
                amount = value
            else:
                self._deny(request_id, GateStage.G0_SCHEMA, "INVALID_TOOL_SPEC",
                           f"declared draw for {name} is not a finite nonnegative number")
            reserved[name] = amount
        return reserved

    def resolve(self, request: ToolCallRequest, spec: ToolSpec) -> ResolvedReservation:
        """D-037 static sizing refined by the trusted request-bound resolver (D-048).

        Only string-declared dimensions may be resolved; the string itself is never
        read. Every resolved value is validated against the ToolSpec maximum.
        """
        request_id = request.request_id
        draw = self.reservation(request_id, spec)
        declared = spec.declared_budget_draw
        dynamic = {name for name, value in declared.items() if isinstance(value, str)}
        exact = {name for name, value in declared.items() if not isinstance(value, str)}
        origins = {name: ReservationOrigin.DECLARED if name in exact
                   else ReservationOrigin.MAX_FALLBACK for name in draw}
        if not dynamic or self.resolver is None:
            return ResolvedReservation(draw, origins)
        failure = None
        try:
            resolved = self.resolver.resolve_reservation(request, spec)
            if isinstance(resolved, Mapping):
                resolved = dict(resolved.items())
        except Exception as exc:  # a resolver that cannot answer denies
            failure = type(exc).__name__  # never the message: consumer internals
        if failure is not None:
            self._deny(request_id, GateStage.G2_BUDGET, "RESOLVER_ERROR",
                       f"reservation resolver failed: {failure}")
        if not isinstance(resolved, dict) or any(not isinstance(k, str) for k in resolved):
            self._deny(request_id, GateStage.G2_BUDGET, "INVALID_RESOLVER_OUTPUT",
                       "reservation resolver must return a mapping of dimension names")
        for name in sorted(resolved):
            value = resolved[name]
            if name not in draw:
                self._deny(request_id, GateStage.G2_BUDGET, "RESOLVED_DIMENSION_UNDECLARED",
                           f"{name} is not declared by the ToolSpec")
            if name not in dynamic:
                self._deny(request_id, GateStage.G2_BUDGET, "RESOLVED_DIMENSION_NOT_DYNAMIC",
                           f"{name} is not a dynamic declared draw")
            if not _finite(value):
                self._deny(request_id, GateStage.G2_BUDGET, "INVALID_RESOLVED_DRAW",
                           f"resolved draw for {name} is not a finite nonnegative number")
            if value > spec.max_budget_draw[name]:
                self._deny(request_id, GateStage.G2_BUDGET, "RESOLVED_DRAW_EXCEEDS_MAX",
                           f"resolved draw for {name} exceeds its maximum")
            draw[name], origins[name] = value, ReservationOrigin.REQUEST_BOUND
        return ResolvedReservation(draw, origins)

    def _side_effect(self, request_id, spec, reservation) -> GateDecision:
        cls = spec.side_effect_class
        simulated = {name for name, amount in reservation.items()
                     if name in self.policy.simulation_dimensions}
        touches_simulation = (set(spec.declared_budget_draw) | set(spec.max_budget_draw)) \
            & self.policy.simulation_dimensions
        if cls == SideEffectClass.ADMIN:
            self._deny(request_id, GateStage.G3_SIDE_EFFECT, "ADMIN_DISABLED",
                       "ADMIN operations are denied in v0")
        if touches_simulation and cls != SideEffectClass.SIMULATE:
            self._deny(request_id, GateStage.G3_SIDE_EFFECT, "SIMULATION_MISCLASSIFIED",
                       "a tool drawing simulation dimensions must be SIMULATE")
        if cls == SideEffectClass.SIMULATE:
            if (spec.isolation_guarantee is None
                    or spec.isolation_guarantee.strip().upper() in _NON_ISOLATING):
                self._deny(request_id, GateStage.G3_SIDE_EFFECT, "NO_ISOLATION_GUARANTEE",
                           "SIMULATE requires a declared isolation guarantee")
            if not simulated or not any(reservation[name] > 0 for name in simulated):
                self._deny(request_id, GateStage.G3_SIDE_EFFECT, "SIMULATION_DRAW_UNDECLARED",
                           "SIMULATE must reserve a configured simulation dimension")
        if cls in (SideEffectClass.SIMULATE, SideEffectClass.PROPOSE, SideEffectClass.MUTATE) \
                and self.reference_guard is None:
            self._deny(request_id, GateStage.G3_SIDE_EFFECT, "REFERENCE_GUARD_REQUIRED",
                       f"{cls.value} requires a reference-state guard")
        return self._decision(request_id, GateStage.G3_SIDE_EFFECT, "SIDE_EFFECT_ELIGIBLE",
                              f"{cls.value} eligible under policy", "ALLOW")

    # -- dynamic budget -------------------------------------------------------------
    @staticmethod
    def remaining(budget: Budget, usage: Mapping[str, float], name: str) -> float:
        if name in STANDARD_DIMENSIONS:
            return getattr(budget, "max_" + name) - usage.get(name, 0)
        if name not in budget.extra_dimensions:
            return -math.inf
        return budget.extra_dimensions[name] - usage.get(name, 0)

    def check_budget(self, request_id: str, draw: Mapping[str, float],
                     usage: Mapping[str, float]) -> GateDecision:
        for name, amount in sorted(draw.items()):
            if name not in STANDARD_DIMENSIONS and name not in self.task.budget.extra_dimensions:
                self._deny(request_id, GateStage.G2_BUDGET, "UNCONFIGURED_DIMENSION",
                           f"{name} has no configured task quota")
            if amount > self.remaining(self.task.budget, usage, name):
                self._deny(request_id, GateStage.G2_BUDGET, "BUDGET_EXHAUSTED",
                           f"{name} draw {amount} exceeds remaining quota")
        return self._decision(request_id, GateStage.G2_BUDGET, "RESERVABLE",
                              "declared draw fits remaining budget", "ALLOW",
                              reserved_budget_draw={k: v for k, v in draw.items()
                                                    if k not in STANDARD_DIMENSIONS})

    # -- full authorization -----------------------------------------------------------
    def authorize(self, request: ToolCallRequest, known_refs: Mapping[str, InformationRef],
                  usage: Mapping[str, float], state_revision: Revision, *,
                  expected_reservation: ResolvedReservation | None = None) -> FrozenRequest:
        """Full pipeline. ``expected_reservation`` is the caller's preflight sizing,
        which authorization must reproduce exactly."""
        spec, resolved, decisions = self.static_check(request, known_refs)
        request_id = request.request_id
        if expected_reservation is not None and expected_reservation != resolved:
            self._deny(request_id, GateStage.G2_BUDGET, "RESERVATION_NOT_DETERMINISTIC",
                       "reservation differs from the preflight reservation")
        reservation = dict(resolved.draw)
        draw = {"tool_calls": 1, "steps": 1, **reservation}
        decisions.insert(2, self.check_budget(request_id, draw, usage))
        reference_revision = None
        if self.reference_guard is not None:
            reference_revision = self.reference_guard.reference_revision()
            if type(reference_revision) not in (int, str):
                self._deny(request_id, GateStage.G3_SIDE_EFFECT, "INVALID_REFERENCE_REVISION",
                           "reference guard returned a non-revision value")
        if self.consumer is None:
            self._deny(request_id, GateStage.CONSUMER, "NO_CONSUMER_VALIDATOR",
                       "executable work requires a consumer validate_request hook")
        consumer = self.consumer.validate_request(request, spec, self.task, state_revision,
                                                  freeze_json(dict(usage)))
        if not isinstance(consumer, GateDecision):
            self._deny(request_id, GateStage.CONSUMER, "INVALID_CONSUMER_DECISION",
                       "consumer validator did not return a GateDecision")
        decisions.append(consumer)
        if consumer.request_id != request_id or consumer.expected_state_revision != state_revision:
            self._deny(request_id, GateStage.CONSUMER, "CONSUMER_DECISION_UNBOUND",
                       "consumer decision is not bound to this request and revision")
        if consumer.decision == "DENY":
            raise GateDenied(consumer)
        if consumer.normalized_request_ref is not None:
            self._deny(request_id, GateStage.CONSUMER, "NORMALIZATION_UNSUPPORTED",
                       "v0 dispatches only the exact validated request")
        if consumer.reserved_budget_draw and dict(consumer.reserved_budget_draw) != reservation:
            self._deny(request_id, GateStage.CONSUMER, "RESERVATION_MISMATCH",
                       "consumer reservation differs from the runtime reservation")
        frozen = FrozenRequest(request, checksum(spec), spec.side_effect_class, reservation,
                               state_revision, reference_revision, self.policy.policy_version,
                               tuple(decisions), resolved.origins)
        needs_approval = (consumer.decision == "REQUIRE_APPROVAL"
                          or spec.side_effect_class in self.policy.approval_required_for)
        if needs_approval:
            verdict = self.approval.decide(frozen) if self.approval is not None else None
            if verdict != "APPROVE":
                self._deny(request_id, GateStage.APPROVAL, "NOT_APPROVED",
                           "approval absent or denied for the frozen request")
            decisions.append(self._decision(
                request_id, GateStage.APPROVAL, "APPROVED", "frozen request approved", "ALLOW",
                expected_state_revision=state_revision))
            frozen = replace(frozen, decisions=tuple(decisions))
        return frozen

    def check_dispatch(self, frozen: FrozenRequest, state_revision: Revision) -> None:
        """Re-verify every binding immediately before the Executor is called."""
        request_id = frozen.request.request_id
        spec = self.specs.get(frozen.request.tool_name)
        if spec is None or checksum(spec) != frozen.spec_checksum:
            self._deny(request_id, GateStage.DISPATCH, "SPEC_CHANGED",
                       "ToolSpec changed after authorization")
        if state_revision != frozen.expected_state_revision:
            self._deny(request_id, GateStage.DISPATCH, "STALE_STATE_REVISION",
                       "task state changed after authorization")
        if frozen.side_effect_class == SideEffectClass.MUTATE:
            if self.reference_guard is None or frozen.expected_reference_revision is None:
                self._deny(request_id, GateStage.DISPATCH, "REFERENCE_REVISION_UNBOUND",
                           "MUTATE requires a bound reference-world revision")
            current = self.reference_guard.reference_revision()
            if current != frozen.expected_reference_revision:
                self._deny(request_id, GateStage.DISPATCH, "STALE_REFERENCE_REVISION",
                           "reference state changed after validation/approval")


@dataclass(frozen=True)
class Reconciliation:
    reserved: Mapping[str, float]
    actual: Mapping[str, float]
    charged: Mapping[str, float]
    violations: tuple[str, ...]


def reconcile(reserved: Mapping[str, float], actual: Any,
              budget: Budget) -> Reconciliation:
    """Charge actual draw against the reservation; flag hidden or excess use.

    A violating result is charged at least its full reservation, so misreporting
    can never make work cheaper than declared.
    """
    violations: list[str] = []
    clean: dict[str, float] = {}
    if not isinstance(actual, Mapping):
        violations.append("actual_budget_draw is not an object")
        actual = {}
    for name, amount in actual.items():
        if not isinstance(name, str) or not _finite(amount) or type(amount) is bool:
            violations.append(f"invalid actual draw for {name!r}")
            continue
        clean[name] = amount
    for name in sorted(clean):
        if name in STANDARD_DIMENSIONS:
            violations.append(f"tool reported runtime-charged dimension {name}")
        elif name not in reserved and clean[name] > 0:
            violations.append(f"unreserved draw on {name}")
    for name, amount in sorted(reserved.items()):
        if name not in clean:
            violations.append(f"reserved dimension {name} not reported")
        elif clean[name] > amount:
            violations.append(f"draw on {name} exceeds reservation")
    charged = {}
    for name in sorted(set(reserved) | set(clean)):
        if name in STANDARD_DIMENSIONS or name not in budget.extra_dimensions:
            continue
        used = clean.get(name, 0)
        charged[name] = max(used, reserved.get(name, 0)) if violations else used
    return Reconciliation(freeze_json(dict(reserved)), freeze_json(clean),
                          freeze_json(charged), tuple(violations))
