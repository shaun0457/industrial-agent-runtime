"""Reference loop: typed routing, atomic updates, B2 gates, and ordered TOOL waves.

Execution is fail-closed without trusted gate, Executor, verifier, and ingestion
hooks. Every executable request passes the B2 GatePipeline (G0-G3, consumer
validate_request, approval) and is re-bound immediately before dispatch. Every
returned result passes the B3 ResultVerificationPipeline before deterministic
ingestion; a rejection never ingests and never makes result refs citable.
SUBTASK is B4.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import math
from typing import Any

from .actions import Action, ModelTurn, ToolCallRequest, WorkBatch
from .contracts import (ContextProjection, InformationRef, Revision, RuntimeResult,
                        SideEffectClass, StateDelta, Task, TaskStatus, ToolResult,
                        ToolSpec, TraceEvent, Visibility)
from .gates import (DEFAULT_POLICY, STANDARD_DIMENSIONS, ApprovalHook, GateDenied,
                    GatePipeline, GatePolicy, ReferenceStateGuard, reconcile)
from .hooks import Executor, ModelProvider, RequestGate, ResultIngestor, ResultVerifier
from .protocols import TaskStateStore
from .serialization import canonical_json, checksum, freeze_json
from .trace import TraceRecorder
from .verification import (INGESTION_ORDER_POLICY, ResultVerificationPipeline,
                           VerificationDecision, VerificationRejected, VerificationStage)


def _traceable(result: Any) -> Any:
    """Untyped executor output is summarized so it can never break the trace."""
    return result if isinstance(result, ToolResult) else {
        "untyped_result": type(result).__name__}


class Denied(ValueError):
    """An auditable deterministic rejection, never an execution retry signal."""


class InvariantViolation(RuntimeError):
    """A breached runtime invariant; the run fails closed instead of continuing."""


TERMINAL_STATUSES = frozenset({TaskStatus.DONE, TaskStatus.FAILED,
                             TaskStatus.EXHAUSTED, TaskStatus.CANCELLED})


class Coordinator:
    """One single-writer run with a consumer state store and bounded fake model.

    Side-effect authority comes only from the trusted ``gate_policy`` (default:
    READ/COMPUTE). SUBTASK and token-metered providers remain fail-closed.
    """

    def __init__(self, task: Task, store: TaskStateStore, provider: ModelProvider,
                 trace: TraceRecorder, tool_specs: Sequence[ToolSpec] = (), *,
                 model_metadata: Mapping[str, Any],
                 gate: RequestGate | None = None,
                 executor: Executor | None = None,
                 verifier: ResultVerifier | None = None,
                 ingestor: ResultIngestor | None = None,
                 gate_policy: GatePolicy | None = None,
                 approval: ApprovalHook | None = None,
                 reference_guard: ReferenceStateGuard | None = None,
                 projection_policy: Mapping[str, Any] | None = None,
                 clock: Callable[[], str] | None = None) -> None:
        self.task, self.store, self.provider, self.trace = task, store, provider, trace
        self.specs = {spec.name: spec for spec in tool_specs}
        if len(self.specs) != len(tool_specs):
            raise ValueError("duplicate ToolSpec name")
        self.metadata = freeze_json(model_metadata)
        for key in ("provider", "model", "model_version", "prompt_template_version"):
            if not isinstance(self.metadata.get(key), str) or not self.metadata[key]:
                raise ValueError(f"model metadata requires {key}")
        self.gate, self.executor, self.verifier, self.ingestor = gate, executor, verifier, ingestor
        self.pipeline = GatePipeline(task, self.specs, gate_policy or DEFAULT_POLICY,
                                     gate, approval, reference_guard)
        self.reference_guard = reference_guard
        self.verification = ResultVerificationPipeline(verifier, ingestor)
        self.policy = freeze_json(projection_policy or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.usage = {"model_calls": 0, "tool_calls": 0, "steps": 0, "subagents": 0,
                      **{name: 0 for name in sorted(task.budget.extra_dimensions)}}
        self._context_refs = {ref.ref_id: ref for ref in task.context_refs}
        self.known_refs = {ref.ref_id: ref for ref in task.context_refs
                           if ref.visibility == Visibility.AGENT}
        self.feedback: list[dict[str, Any]] = []
        self._event_count = 0
        self._ran = False
        self._turn_ids: set[str] = set()
        self._request_ids: set[str] = set()
        self.status = TaskStatus.RUNNING

    def _event(self, kind: str, status: str, inputs: Any = None,
               outputs: Any = None, budget_delta: Mapping[str, int] | None = None,
               **fields: Any) -> None:
        self._event_count += 1
        self.trace.append(TraceEvent(
            f"event-{self._event_count:06d}", self.task.task_id, kind, self.clock(),
            status, budget_delta or {}, inputs, outputs,
            parent_task_id=self.task.parent_task_id, **fields))

    def _deny(self, stage: str, reason: str, **ids: Any) -> None:
        feedback = {"stage": stage, "status": "DENIED", "reason": reason, **ids}
        self.feedback.append(feedback)
        self._event("MODEL_REJECTION" if stage == "MODEL_TURN" else stage,
                    "DENIED", outputs=feedback)

    def _remaining(self, dimension: str) -> float:
        return GatePipeline.remaining(self.task.budget, self.usage, dimension)

    def _charge(self, **draw: int) -> None:
        if any(amount > self._remaining(name) for name, amount in draw.items()):
            raise Denied("standard budget exhausted")
        for name, amount in draw.items():
            self.usage[name] += amount

    def _project(self) -> ContextProjection:
        projection = self.store.project(self.policy)
        if (not isinstance(projection, ContextProjection)
                or projection.task_id != self.task.task_id
                or projection.base_revision != self.store.revision()):
            raise Denied("invalid consumer projection identity/revision")
        # Runtime feedback is explicit input, never a hidden transcript side effect.
        content = {"task_state": projection.content, "runtime_feedback": self.feedback}
        return replace(projection,
                       projection_id=f"{projection.projection_id}:turn-{self.usage['model_calls'] + 1}",
                       content=content, checksum=checksum(content),
                       approximate_tokens=projection.approximate_tokens +
                       len(canonical_json(self.feedback)))

    def _update(self, turn: ModelTurn, projection: ContextProjection,
                ref: InformationRef, turn_error: str | None) -> bool:
        proposal = turn.state_update
        if proposal is None:
            return turn_error is None
        step_draw = 0
        current = self.store.revision()
        try:
            self._charge(steps=1)
            step_draw = 1
            if turn_error:
                raise Denied(turn_error)
            if (type(proposal.base_revision) not in (int, str)
                    or proposal.base_revision != projection.base_revision):
                raise Denied("proposal base revision differs from model projection")
            if any(not isinstance(delta, StateDelta)
                   or type(delta.proposed_base_revision) not in (int, str)
                   or delta.proposed_base_revision != projection.base_revision
                   or not isinstance(delta.operation, str) or not delta.operation
                   or not isinstance(delta.target_ref_or_path, str)
                   for delta in proposal.deltas):
                raise Denied("malformed delta or missing/mismatched projection revision")
            if any(delta.reason_ref is not None and
                   delta.reason_ref.visibility != Visibility.AGENT for delta in proposal.deltas):
                raise Denied("hidden reason reference")
            # The model cannot impersonate deterministic ingestion/runtime control.
            deltas = tuple(replace(delta, producer="MODEL") for delta in proposal.deltas)
            current = self.store.apply_batch(deltas, expected_revision=projection.base_revision)
        except Exception as exc:
            reason = str(exc)
            self._event("STATE_UPDATE", "DENIED", inputs={
                "proposal": proposal, "projection_ref": ref,
                "projection_base_revision": projection.base_revision},
                outputs={"reason": reason, "operations": [
                    delta.operation for delta in proposal.deltas if isinstance(delta, StateDelta)]},
                budget_delta={"steps": step_draw})
            self.feedback.append({"stage": "STATE_UPDATE", "status": "DENIED",
                                  "proposal_id": proposal.proposal_id, "reason": reason})
            return False
        self._event("STATE_UPDATE", "ACCEPTED", inputs={
            "proposal": proposal, "projection_ref": ref,
            "projection_base_revision": projection.base_revision},
            outputs={"operations": [delta.operation for delta in deltas],
                     "resulting_revision": current}, budget_delta={"steps": 1})
        return True

    def _request_supported(self, request: ToolCallRequest) -> tuple[ToolSpec, dict[str, float]]:
        """Static G0/G1/G3 checks plus reservation sizing; no state or budget change."""
        spec, reservation, _ = self.pipeline.static_check(request, self.known_refs)
        if request.request_id in self._request_ids:
            raise Denied("request_id already dispatched; use a new explicit request")
        if any(hook is None for hook in (self.gate, self.executor, self.verifier, self.ingestor)):
            raise Denied("execution requires explicit gate/executor/verifier/ingestor hooks")
        return spec, reservation

    def _reference_revision(self) -> Revision | None:
        return None if self.reference_guard is None else self.reference_guard.reference_revision()

    def _execute(self, request: ToolCallRequest, **ids: Any):
        """Authorize, dispatch once, and reconcile. Returns (result, accounting) or None."""
        request_id = getattr(request, "request_id", None)
        try:
            spec, _ = self._request_supported(request)
            frozen = self.pipeline.authorize(request, self.known_refs, self.usage,
                                             self.store.revision())
            for decision in frozen.decisions:
                self._event("GATE", decision.decision, inputs=request, outputs=decision, **ids)
            self.pipeline.check_dispatch(frozen, self.store.revision())
            # Baseline for the no-reference-mutation invariant; a guard failure here
            # is an ordinary pre-dispatch denial, before any budget is charged.
            before = (self._reference_revision()
                      if spec.side_effect_class != SideEffectClass.MUTATE else None)
        except GateDenied as exc:
            self._event("GATE", exc.decision.decision, inputs=request,
                        outputs=exc.decision, **ids)
            self._deny("EXECUTION", str(exc), request_id=request_id,
                       reason_code=exc.decision.reason_code, **ids)
            return None
        except Denied as exc:
            self._deny("EXECUTION", str(exc), request_id=request_id, **ids)
            return None
        except Exception as exc:  # a gate that cannot decide denies
            self._deny("EXECUTION", f"GATE_ERROR: {exc}", request_id=request_id, **ids)
            return None
        reserved = frozen.reserved_budget_draw
        self._charge(tool_calls=1, steps=1)
        self._request_ids.add(request.request_id)
        self._event("EXECUTE", "DISPATCHED", inputs={"request": request, "reserved": reserved},
                    budget_delta={"tool_calls": 1, "steps": 1}, **ids)
        result, error = None, None
        try:
            result = self.executor.execute(request, spec)
        except Exception as exc:
            result, error = None, str(exc)
        # Unknown actual use (adapter failure or an untyped/misbound result) is charged
        # at the full reservation; identity itself is judged by B3 verification.
        bound = isinstance(result, ToolResult) and result.request_id == request.request_id
        accounting = reconcile(reserved, result.actual_budget_draw if bound else reserved,
                               self.task.budget)
        for name, amount in accounting.charged.items():
            self.usage[name] += amount
        self._event("RECONCILIATION", "VIOLATION" if accounting.violations else "RECONCILED",
                    outputs=accounting, budget_delta=accounting.charged, **ids)
        if before is not None and self._reference_revision() != before:
            raise InvariantViolation(f"{spec.side_effect_class.value} request "
                                     f"{request.request_id} changed reference state")
        if error is not None:
            self._deny("EXECUTION", error, request_id=request.request_id, **ids)
            return None
        self._event("TOOL_RESULT", "RETURNED", outputs=_traceable(result), **ids)
        return result, accounting

    def _ingest(self, request: ToolCallRequest, executed, sequence: int = 0,
                **ids: Any) -> bool:
        """B3 verification, then atomic ingestion bound to the CURRENT revision."""
        result, accounting = executed
        revision = None
        try:
            revision = self.store.revision()
            reserved = {**self._context_refs, **self.known_refs}
            verified = self.verification.verify(
                result, request, self.specs[request.tool_name], accounting, self.known_refs,
                reserved, revision, self.store.revision)
        except Exception as exc:  # any verification failure rejects only this result
            decision = exc.decision if isinstance(exc, VerificationRejected) else \
                VerificationDecision(request.request_id, "REJECT", "VERIFICATION",
                                     "VERIFICATION_ERROR", str(exc),
                                     expected_state_revision=revision)
            self._event("VERIFY_RESULT", "REJECTED", inputs=_traceable(result),
                        outputs=decision, **ids)
            self._deny("VERIFY_RESULT", str(exc), request_id=request.request_id,
                       reason_code=decision.reason_code, **ids)
            return False
        self._event("VERIFY_RESULT", "ACCEPTED", inputs=result, outputs={
            "decision": verified.decision, "deltas": verified.deltas,
            "expected_revision": revision}, **ids)
        ingestion = {"deltas": verified.deltas, "expected_revision": revision,
                     "order_policy": INGESTION_ORDER_POLICY, "sequence": sequence}
        try:
            new_revision = (self.store.apply_batch(verified.deltas, expected_revision=revision)
                            if verified.deltas else revision)
        except Exception as exc:  # consumer rejected the verified batch: nothing applied
            self._event("RESULT_INGESTION", "REJECTED", inputs=ingestion,
                        outputs={"error": str(exc)}, **ids)
            self._deny("RESULT_INGESTION", str(exc), request_id=request.request_id, **ids)
            return False
        if new_revision != self.store.revision():
            raise InvariantViolation("apply_batch returned a revision that differs from the store")
        for ref in verified.refs:  # verified Agent-visible refs become citable in later requests
            self.known_refs.setdefault(ref.ref_id, ref)
        self._event("RESULT_INGESTION", "ACCEPTED", inputs=ingestion,
                    outputs={"resulting_revision": new_revision}, **ids)
        return True

    def _validate_batch(self, batch: WorkBatch) -> None:
        if batch.completion_policy != "ALL_SETTLED":
            raise Denied("unsupported completion_policy")
        items = {item.work_id: item for item in batch.items}
        if len(items) != len(batch.items):
            raise Denied("duplicate work_id")
        request_ids = set()
        reserved: dict[str, float] = {}
        for item in batch.items:
            if item.kind != "TOOL":
                raise Denied("SUBTASK execution requires downstream B4 implementation")
            if item.status != "PENDING":
                raise Denied("model may not pre-complete work")
            _, draw = self._request_supported(item.request_or_subtask)
            for name, amount in draw.items():
                reserved[name] = reserved.get(name, 0) + amount
            request_id = item.request_or_subtask.request_id
            if request_id in request_ids:
                raise Denied("duplicate request_id in batch")
            request_ids.add(request_id)
            if any(dependency not in items for dependency in item.depends_on):
                raise Denied("unknown dependency")
        pending, visited = set(items), set()
        while pending:
            ready = {key for key in pending if set(items[key].depends_on) <= visited}
            if not ready:
                raise Denied("cyclic WorkBatch")
            pending -= ready
            visited |= ready
        if len(items) > min(self._remaining("tool_calls"), self._remaining("steps")):
            raise Denied("cumulative WorkBatch budget exceeds remaining quota")
        for draw in (batch.budget_request, *(item.budget_request for item in batch.items)):
            if not isinstance(draw, Mapping):
                raise Denied("budget_request must be an object")
            for name, amount in draw.items():
                if type(amount) not in (int, float) or not math.isfinite(amount) or amount < 0:
                    raise Denied("invalid requested budget")
                if ((name in STANDARD_DIMENSIONS and name not in {"tool_calls", "steps"})
                        or amount > self._remaining(name)):
                    raise Denied("unsupported/excessive WorkBatch budget request")
        for name in sorted({"tool_calls", "steps", *self.task.budget.extra_dimensions}):
            if sum(item.budget_request.get(name, 0) for item in batch.items) > self._remaining(name):
                raise Denied("cumulative WorkItem budget exceeds remaining quota")
        # Cumulative G2 preflight: all declared item reservations must fit together.
        self.pipeline.check_budget(batch.batch_id, {
            "tool_calls": len(items), "steps": len(items), **reserved}, self.usage)

    def _batch(self, batch: WorkBatch) -> None:
        try:
            self._validate_batch(batch)
        except Exception as exc:
            self._deny("WORK_BATCH", str(exc), batch_id=batch.batch_id)
            return
        pending = {item.work_id: item for item in batch.items}
        outcomes: dict[str, str] = {}
        wave = 0
        while pending:
            ready = sorted(key for key, item in pending.items()
                           if all(dependency in outcomes for dependency in item.depends_on))
            collected = []
            for key in ready:
                item = pending.pop(key)
                if any(outcomes[dependency] != "COMPLETED" for dependency in item.depends_on):
                    outcomes[key] = "SKIPPED_DEPENDENCY"
                    self._event("WORK_ITEM", outcomes[key], batch_id=batch.batch_id, work_id=key)
                    continue
                result = self._execute(item.request_or_subtask, batch_id=batch.batch_id, work_id=key)
                collected.append((key, item.request_or_subtask, result))
            # The sequential executor is permitted by OQ-3. All results from a
            # scheduling wave are collected before deterministic lexical ingestion.
            wave += 1
            order = [key for key, _, result in collected if result is not None]
            if order:
                self._event("INGESTION_ORDER", "PLANNED", batch_id=batch.batch_id, outputs={
                    "policy": INGESTION_ORDER_POLICY, "wave": wave, "order": order})
            for key, request, result in collected:
                success = result is not None and self._ingest(
                    request, result, order.index(key) if result is not None else -1,
                    batch_id=batch.batch_id, work_id=key)
                outcomes[key] = "COMPLETED" if success else "FAILED"
                self._event("WORK_ITEM", outcomes[key], batch_id=batch.batch_id, work_id=key)
        self.feedback.append({"stage": "WORK_BATCH", "batch_id": batch.batch_id,
                              "completion_policy": "ALL_SETTLED", "outcomes": outcomes})
        self._event("WORK_BATCH", "ALL_SETTLED", outputs=outcomes, batch_id=batch.batch_id)

    def run(self) -> RuntimeResult:
        if self._ran or self.trace.read_events():
            raise ValueError("Coordinator requires an unused run/trace directory")
        self._ran = True
        output = None
        errors: list[str] = []
        self._event("TASK", "RUNNING", inputs=self.task)
        if self.store.status() in TERMINAL_STATUSES:
            self.status = self.store.status()
        elif self.task.budget.max_total_tokens is not None:
            self.status = TaskStatus.FAILED
            errors.append("token-metered provider execution requires downstream accounting")
        while self.status == TaskStatus.RUNNING:
            if self.store.status() in TERMINAL_STATUSES:
                self.status = self.store.status()
                break
            if self._remaining("model_calls") <= 0 or self._remaining("steps") <= 0:
                self.status = TaskStatus.EXHAUSTED
                break
            try:
                projection = self._project()
                ref = self.trace.persist_projection(projection, self.clock())
                self.feedback = []
                tools = tuple(spec for name, spec in self.specs.items()
                              if name in self.task.allowed_tools)
                self._charge(model_calls=1)
                metadata = {key: self.metadata[key] for key in (
                    "provider", "model", "model_version", "prompt_template_version")}
                self._event("MODEL_TURN", "REQUESTED", inputs={"projection_ref": ref},
                            budget_delta={"model_calls": 1}, context_projection_ref=ref,
                            sampling_parameters=self.metadata.get("sampling_parameters", {}),
                            registered_tool_set_version=checksum(tools), **metadata)
                turn = self.provider.generate(projection, tools, self.task.output_schema, {
                    "context_projection_ref": ref, "budget": self.task.budget,
                    "budget_usage": freeze_json(self.usage)})
                if not isinstance(turn, ModelTurn):
                    raise Denied("provider returned non-ModelTurn output")
                self._event("MODEL_OUTPUT", "RETURNED", outputs=turn)
                turn_error = None
                if (turn.context_projection_ref != ref or turn.base_revision != projection.base_revision
                        or turn.turn_id in self._turn_ids):
                    turn_error = "model turn projection/revision/identity mismatch"
                self._turn_ids.add(turn.turn_id)
                accepted = self._update(turn, projection, ref, turn_error)
                if not accepted:
                    if turn_error and turn.state_update is None:
                        self._deny("MODEL_TURN", turn_error)
                    continue
                if turn.action == Action.TOOL_REQUEST:
                    result = self._execute(turn.tool_request)
                    if result is not None:
                        success = self._ingest(turn.tool_request, result)
                        self.feedback.append({"stage": "TOOL_REQUEST",
                                              "request_id": turn.tool_request.request_id,
                                              "status": "COMPLETED" if success else "FAILED"})
                elif turn.action == Action.WORK_BATCH:
                    self._batch(turn.work_batch)
                elif turn.action == Action.FINISH_PROPOSAL:
                    proposal = turn.finish_proposal
                    if self.verifier is None:
                        raise Denied("finish requires a trusted result verifier")
                    try:
                        decision = self.verification.verify_finish(
                            proposal, self.task, self.known_refs, self.store.revision())
                    except VerificationRejected as exc:
                        self._event("VERIFY_FINISH", "REJECTED", inputs=proposal,
                                    outputs=exc.decision)
                        raise Denied(str(exc)) from exc
                    revision = self.store.revision()
                    try:
                        verdict = self.verifier.verify_finish(proposal, self.task, revision)
                    except Exception as exc:  # B1 semantics: the run fails closed
                        self._event("VERIFY_FINISH", "REJECTED", inputs=proposal,
                                    outputs=VerificationDecision(
                                        self.task.task_id, "REJECT",
                                        VerificationStage.CONSUMER.value, "VERIFIER_ERROR",
                                        str(exc), expected_state_revision=revision))
                        raise
                    if verdict is not True:
                        self._event("VERIFY_FINISH", "REJECTED", inputs=proposal,
                                    outputs=VerificationDecision(
                                        self.task.task_id, "REJECT",
                                        VerificationStage.CONSUMER.value, "CONSUMER_REJECTED",
                                        "consumer verify_finish did not return True",
                                        expected_state_revision=revision,
                                        passed_stages=decision.passed_stages))
                        raise Denied("consumer finish verification denied")
                    self._event("VERIFY_FINISH", "ACCEPTED", inputs=proposal, outputs=decision)
                    self._event("FINISH", "ACCEPTED", inputs=proposal)
                    output, self.status = proposal.structured_output, TaskStatus.DONE
                else:
                    self._event("ACTION", "NONE")
            except Denied as exc:
                self._deny("MODEL_TURN", str(exc))
            except Exception as exc:
                self.status = TaskStatus.FAILED
                errors.append(str(exc))
                self._event("TASK_FAILURE", "FAILED", outputs={"error": str(exc)})
        persistence_error = self._persist_terminal_status()
        if persistence_error is not None:
            self.status, output = TaskStatus.FAILED, None
            errors.append(persistence_error)
        self._event("TASK", self.status.value, outputs={
            "state_revision": self.store.revision(), "budget_usage": self.usage, "errors": errors})
        payload = self.trace.events_path.read_bytes()
        trace_ref = InformationRef("events.jsonl", "RunTrace", "industrial-agent-runtime", "v0",
                                   Visibility.INTERNAL, self.clock(), hashlib.sha256(payload).hexdigest())
        return RuntimeResult(self.task.task_id, self.status, output, self.store.revision(),
                             trace_ref, self.usage, errors=tuple(errors))

    def _persist_terminal_status(self) -> str | None:
        requested = self.status
        revision = self.store.revision()
        prior_status = self.store.status()
        inputs = {"requested_status": requested, "expected_revision": revision,
                  "prior_status": prior_status}
        try:
            if requested not in TERMINAL_STATUSES:
                raise Denied("only terminal statuses may be persisted here")
            returned = self.store.transition_status(requested, expected_revision=revision)
            observed_revision, observed_status = self.store.revision(), self.store.status()
            if (type(returned) not in (int, str) or returned != observed_revision
                    or observed_status != requested):
                raise Denied("status transition returned inconsistent status/revision")
            if (returned == revision) != (prior_status == requested):
                raise Denied("status transition violated revision advancement/no-op semantics")
        except Exception as exc:
            error = f"STATUS_PERSISTENCE_FAILED: {exc}"
            self._event("STATUS_TRANSITION", "DENIED", inputs=inputs, outputs={
                "observed_status": self.store.status(), "observed_revision": self.store.revision(),
                "error": error})
            return error
        self._event("STATUS_TRANSITION", "ACCEPTED", inputs=inputs, outputs={
            "resulting_status": observed_status, "resulting_revision": observed_revision})
        return None
