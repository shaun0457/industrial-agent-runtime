"""B1 reference loop: typed routing, atomic updates, and ordered TOOL waves.

Execution is fail-closed without trusted gate, Executor, verifier, and ingestion
hooks. This intentionally restricted B1 surface is not the full B2/B3 runtime.
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
from .hooks import Executor, GateDecision, ModelProvider, RequestGate, ResultIngestor, ResultVerifier
from .protocols import TaskStateStore
from .serialization import canonical_json, checksum, freeze_json
from .trace import TraceRecorder


class Denied(ValueError):
    """An auditable deterministic rejection, never an execution retry signal."""


def _has_hidden_ref(value: Any) -> bool:
    """Inspect JSON ref envelopes; semantic/string-ref resolution belongs to B2."""
    if isinstance(value, Mapping):
        if "ref_id" in value and "visibility" in value and value["visibility"] != "AGENT":
            return True
        return any(_has_hidden_ref(item) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_has_hidden_ref(item) for item in value)
    return False


class Coordinator:
    """One single-writer run with a consumer state store and bounded fake model.

    B1 supports READ/COMPUTE tools with zero declared/actual extra resource draw.
    SIMULATE/PROPOSE/MUTATE/ADMIN, SUBTASK, and token-metered providers remain
    fail-closed until the downstream implementations provide those capabilities.
    """

    def __init__(self, task: Task, store: TaskStateStore, provider: ModelProvider,
                 trace: TraceRecorder, tool_specs: Sequence[ToolSpec] = (), *,
                 model_metadata: Mapping[str, Any],
                 gate: RequestGate | None = None,
                 executor: Executor | None = None,
                 verifier: ResultVerifier | None = None,
                 ingestor: ResultIngestor | None = None,
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
        self.policy = freeze_json(projection_policy or {})
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        self.usage = {"model_calls": 0, "tool_calls": 0, "steps": 0, "subagents": 0}
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

    def _remaining(self, dimension: str) -> int:
        return getattr(self.task.budget, "max_" + dimension) - self.usage[dimension]

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

    def _request_supported(self, request: ToolCallRequest) -> ToolSpec:
        if not isinstance(request, ToolCallRequest):
            raise Denied("typed ToolCallRequest required")
        if request.tool_name not in self.task.allowed_tools or request.tool_name not in self.specs:
            raise Denied("unknown or unlisted tool")
        if request.request_id in self._request_ids:
            raise Denied("request_id already dispatched; use a new explicit request")
        if _has_hidden_ref(request.arguments):
            raise Denied("executable request contains a hidden reference envelope")
        spec = self.specs[request.tool_name]
        if spec.side_effect_class not in {SideEffectClass.READ, SideEffectClass.COMPUTE}:
            raise Denied("side-effect class requires downstream B2 implementation")
        if spec.declared_budget_draw or spec.max_budget_draw:
            raise Denied("resource reservation requires downstream B2 implementation")
        if any(hook is None for hook in (self.gate, self.executor, self.verifier, self.ingestor)):
            raise Denied("execution requires explicit gate/executor/verifier/ingestor hooks")
        return spec

    def _execute(self, request: ToolCallRequest, **ids: Any) -> ToolResult | None:
        try:
            spec = self._request_supported(request)
            if self._remaining("tool_calls") < 1 or self._remaining("steps") < 1:
                raise Denied("tool/step budget exhausted")
            revision = self.store.revision()
            decision = self.gate.validate_request(
                request, spec, self.task, revision, freeze_json(self.usage))
            if not isinstance(decision, GateDecision):
                raise Denied("invalid gate decision")
            self._event("GATE", decision.decision, inputs=request, outputs=decision, **ids)
            if (decision.request_id != request.request_id or decision.decision != "ALLOW"
                    or decision.expected_state_revision != revision
                    or decision.reserved_budget_draw or decision.normalized_request_ref is not None):
                raise Denied("gate denied, unbound, or requires unsupported reservation/normalization")
            if self.store.revision() != revision:
                raise Denied("state changed during authorization")
            self._charge(tool_calls=1, steps=1)
            self._request_ids.add(request.request_id)
            self._event("EXECUTE", "DISPATCHED", inputs=request,
                        budget_delta={"tool_calls": 1, "steps": 1}, **ids)
            result = self.executor.execute(request, spec)
            if not isinstance(result, ToolResult) or result.request_id != request.request_id:
                raise Denied("executor returned malformed/misbound result")
            self._event("TOOL_RESULT", "RETURNED", outputs=result, **ids)
            return result
        except Exception as exc:
            self._deny("EXECUTION", str(exc), request_id=request.request_id, **ids)
            return None

    def _ingest(self, request: ToolCallRequest, result: ToolResult, **ids: Any) -> bool:
        try:
            if result.status != "SUCCESS":
                raise Denied("tool did not succeed")
            if any(ref.visibility != Visibility.AGENT
                   for ref in (*result.information_refs, *result.artifact_refs)):
                raise Denied("result exposes hidden reference")
            if any(type(value) not in (int, float) or not math.isfinite(value) or value != 0
                   for value in result.actual_budget_draw.values()):
                raise Denied("unreserved resource use in B1 result")
            revision = self.store.revision()
            deltas = tuple(replace(delta, producer="RESULT_INGESTION",
                                   proposed_base_revision=revision)
                           for delta in self.ingestor.derive_deltas(result))
            if self.verifier.verify_result(result, request, self.specs[request.tool_name],
                                           deltas, revision) is not True:
                raise Denied("post-execution verification denied")
            if self.store.revision() != revision:
                raise Denied("state changed during verification/ingestion preparation")
            self._event("VERIFY_RESULT", "ACCEPTED", inputs=result, outputs={
                "deltas": deltas, "expected_revision": revision}, **ids)
            new_revision = (self.store.apply_batch(deltas, expected_revision=revision)
                            if deltas else revision)
            self._event("RESULT_INGESTION", "ACCEPTED", inputs={
                "deltas": deltas, "expected_revision": revision},
                outputs={"resulting_revision": new_revision, "order": "work_id_lexical_per_wave"},
                **ids)
            return True
        except Exception as exc:
            self._deny("VERIFY_RESULT", str(exc), request_id=request.request_id, **ids)
            return False

    def _validate_batch(self, batch: WorkBatch) -> None:
        if batch.completion_policy != "ALL_SETTLED":
            raise Denied("unsupported completion_policy")
        items = {item.work_id: item for item in batch.items}
        if len(items) != len(batch.items):
            raise Denied("duplicate work_id")
        request_ids = set()
        for item in batch.items:
            if item.kind != "TOOL":
                raise Denied("SUBTASK execution requires downstream B4 implementation")
            if item.status != "PENDING":
                raise Denied("model may not pre-complete work")
            self._request_supported(item.request_or_subtask)
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
                if name not in {"tool_calls", "steps"} or amount > self._remaining(name):
                    raise Denied("unsupported/excessive WorkBatch budget request")
        for name in ("tool_calls", "steps"):
            if sum(item.budget_request.get(name, 0) for item in batch.items) > self._remaining(name):
                raise Denied("cumulative WorkItem budget exceeds remaining quota")

    def _batch(self, batch: WorkBatch) -> None:
        try:
            self._validate_batch(batch)
        except Exception as exc:
            self._deny("WORK_BATCH", str(exc), batch_id=batch.batch_id)
            return
        pending = {item.work_id: item for item in batch.items}
        outcomes: dict[str, str] = {}
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
            for key, request, result in collected:
                success = result is not None and self._ingest(
                    request, result, batch_id=batch.batch_id, work_id=key)
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
        if self.task.budget.max_total_tokens is not None:
            self.status = TaskStatus.FAILED
            errors.append("token-metered provider execution requires downstream accounting")
        while self.status == TaskStatus.RUNNING:
            if self.store.status() in {TaskStatus.CANCELLED, TaskStatus.FAILED, TaskStatus.EXHAUSTED}:
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
                    if (self.verifier is None or any(ref.visibility != Visibility.AGENT for ref in
                            (*proposal.information_refs, *proposal.artifact_refs))
                            or self.verifier.verify_finish(proposal, self.task, self.store.revision())
                            is not True):
                        raise Denied("finish structural verification denied")
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
        self._event("TASK", self.status.value, outputs={
            "state_revision": self.store.revision(), "budget_usage": self.usage, "errors": errors})
        payload = self.trace.events_path.read_bytes()
        trace_ref = InformationRef("events.jsonl", "RunTrace", "industrial-agent-runtime", "v0",
                                   Visibility.INTERNAL, self.clock(), hashlib.sha256(payload).hexdigest())
        return RuntimeResult(self.task.task_id, self.status, output, self.store.revision(),
                             trace_ref, self.usage, errors=tuple(errors))
