"""B3 deterministic post-execution result verification (hybrid-orchestration-v0).

V0 identity -> V1 provenance -> V2 output schema -> V3 refs/visibility ->
V4 accounting -> V5 ingestion structure -> consumer verify_result -> V6 revision.

Every stage is mechanical: no model input, no open-ended critique, no domain
knowledge. Any rejection prevents ingestion and keeps result refs uncitable.
Pre-execution authorization (G0-G3) is B2 and is not repeated here.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .actions import FinishProposal, ToolCallRequest
from .contracts import InformationRef, Revision, StateDelta, Task, ToolResult, ToolSpec, Visibility
from .gates import _REF_FIELDS, Reconciliation, ref_envelopes
from .hooks import ResultIngestor, ResultVerifier
from .schema import instance_errors, schema_errors

VERIFIER_VERSION = "runtime-result-verifier-v0"
RESULT_SUCCESS = "SUCCESS"
REQUIRED_PROVENANCE_FIELDS = ("tool_version",)
INGESTION_PRODUCER = "RESULT_INGESTION"
INGESTION_ORDER_POLICY = "work_id_lexical_per_wave"


class VerificationStage(StrEnum):
    V0_IDENTITY = "V0_IDENTITY"
    V1_PROVENANCE = "V1_PROVENANCE"
    V2_OUTPUT_SCHEMA = "V2_OUTPUT_SCHEMA"
    V3_REFS = "V3_REFS"
    V4_ACCOUNTING = "V4_ACCOUNTING"
    V5_INGESTION_STRUCTURE = "V5_INGESTION_STRUCTURE"
    CONSUMER = "CONSUMER"
    V6_REVISION = "V6_REVISION"
    FINISH = "FINISH"


@dataclass(frozen=True)
class VerificationDecision:
    """Auditable outcome of one verification. Only ``ACCEPT`` permits ingestion."""

    subject_id: str
    decision: str
    stage: str
    reason_code: str
    reason: str
    verifier_version: str = VERIFIER_VERSION
    expected_state_revision: Revision | None = None
    passed_stages: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {"ACCEPT", "REJECT"}:
            raise ValueError("unknown verification decision")
        object.__setattr__(self, "passed_stages", tuple(self.passed_stages))


class VerificationRejected(ValueError):
    """Structured fail-closed rejection; carries the VerificationDecision to trace."""

    def __init__(self, decision: VerificationDecision) -> None:
        super().__init__(f"{decision.stage}/{decision.reason_code}: {decision.reason}")
        self.decision = decision


@dataclass(frozen=True)
class VerifiedResult:
    decision: VerificationDecision
    deltas: tuple[StateDelta, ...]
    refs: tuple[InformationRef, ...]


class _Run:
    """Accumulates passed stages so a rejection reports how far checking got."""

    def __init__(self, subject_id: str, revision: Revision | None) -> None:
        self.subject_id, self.revision, self.passed = subject_id, revision, []

    def reject(self, stage: VerificationStage, code: str, reason: str):
        raise VerificationRejected(VerificationDecision(
            self.subject_id, "REJECT", stage.value, code, reason,
            expected_state_revision=self.revision, passed_stages=tuple(self.passed)))

    def ok(self, stage: VerificationStage) -> None:
        self.passed.append(stage.value)

    def accept(self, reason: str) -> VerificationDecision:
        return VerificationDecision(self.subject_id, "ACCEPT", "VERIFIED", "VERIFIED", reason,
                                    expected_state_revision=self.revision,
                                    passed_stages=tuple(self.passed))


def _is_ref(value: Any) -> bool:
    return (isinstance(value, InformationRef)
            and all(isinstance(getattr(value, name), str) and getattr(value, name)
                    for name in ("ref_id", "kind", "owner", "version", "created_at"))
            and isinstance(value.visibility, Visibility)
            and (value.checksum is None or (isinstance(value.checksum, str) and value.checksum)))


def check_refs(run: _Run, refs: Sequence[Any], embedded: Sequence[Mapping[str, Any]],
               known: Mapping[str, InformationRef], reserved: Mapping[str, InformationRef],
               *, require_known: bool) -> tuple[InformationRef, ...]:
    """Structure, visibility, id uniqueness, and (optionally) prior knowledge of refs."""
    stage = VerificationStage.V3_REFS if not require_known else VerificationStage.FINISH
    seen: dict[str, InformationRef] = {}
    for ref in refs:
        if not _is_ref(ref):
            run.reject(stage, "MALFORMED_REF", "reference is not a well-formed InformationRef")
        if ref.visibility != Visibility.AGENT:
            run.reject(stage, "HIDDEN_REF", "non-AGENT reference cannot reach the Agent plane")
        if seen.get(ref.ref_id, ref) != ref:
            run.reject(stage, "DUPLICATE_REF_ID", f"ref id {ref.ref_id!r} is declared twice")
        if reserved.get(ref.ref_id, ref) != ref:
            run.reject(stage, "REF_ID_CONFLICT",
                       f"ref id {ref.ref_id!r} conflicts with a known or hidden context ref")
        if require_known and known.get(ref.ref_id) != ref:
            run.reject(stage, "UNKNOWN_REF",
                       f"ref {ref.ref_id!r} is not a verified Agent-visible ref of this run")
        seen[ref.ref_id] = ref
    for envelope in embedded:
        if envelope.get("visibility") != Visibility.AGENT.value:
            run.reject(stage, "HIDDEN_REF_IN_OUTPUT", "structured output embeds a non-AGENT ref")
        try:
            if not set(envelope) <= _REF_FIELDS:
                raise ValueError("unknown ref fields")
            embedded_ref = InformationRef(**envelope)
        except (TypeError, ValueError):
            run.reject(stage, "MALFORMED_REF", "embedded ref is not a valid InformationRef")
        # Same rule as the B2 gate: the whole envelope must equal the real ref.
        if embedded_ref not in (seen.get(embedded_ref.ref_id), known.get(embedded_ref.ref_id)):
            run.reject(stage, "UNDECLARED_REF_IN_OUTPUT",
                       "embedded ref is neither declared nor an exact known ref")
    return tuple(seen.values())


class ResultVerificationPipeline:
    """Deterministic post-execution checks plus the consumer verify_result hook."""

    def __init__(self, verifier: ResultVerifier, ingestor: ResultIngestor) -> None:
        self.verifier, self.ingestor = verifier, ingestor

    def verify(self, result: Any, request: ToolCallRequest, spec: ToolSpec,
               accounting: Reconciliation, known: Mapping[str, InformationRef],
               reserved: Mapping[str, InformationRef], current_revision: Revision,
               revision_reader) -> VerifiedResult:
        run = _Run(request.request_id, current_revision)
        if not isinstance(result, ToolResult):
            run.reject(VerificationStage.V0_IDENTITY, "UNTYPED_RESULT", "ToolResult required")
        if result.request_id != request.request_id:
            run.reject(VerificationStage.V0_IDENTITY, "MISBOUND_RESULT",
                       "result request_id differs from the dispatched request")
        if result.status != RESULT_SUCCESS:
            run.reject(VerificationStage.V0_IDENTITY, "TOOL_NOT_SUCCEEDED",
                       f"tool status {result.status!r} is not ingestible")
        run.ok(VerificationStage.V0_IDENTITY)

        provenance = result.provenance
        if not isinstance(provenance, Mapping):
            run.reject(VerificationStage.V1_PROVENANCE, "MISSING_PROVENANCE",
                       "provenance must be an object")
        for name in REQUIRED_PROVENANCE_FIELDS:
            if not isinstance(provenance.get(name), str) or not provenance[name]:
                run.reject(VerificationStage.V1_PROVENANCE, "MISSING_PROVENANCE_FIELD",
                           f"provenance requires a nonempty {name}")
        for name, expected in (("request_id", request.request_id),
                               ("tool_name", request.tool_name)):
            if name in provenance and provenance[name] != expected:
                run.reject(VerificationStage.V1_PROVENANCE, "PROVENANCE_MISMATCH",
                           f"provenance {name} does not match the dispatched request")
        run.ok(VerificationStage.V1_PROVENANCE)

        problems = schema_errors(spec.output_schema) or instance_errors(
            result.structured_output, spec.output_schema)
        if problems:
            run.reject(VerificationStage.V2_OUTPUT_SCHEMA, "INVALID_OUTPUT", "; ".join(problems))
        run.ok(VerificationStage.V2_OUTPUT_SCHEMA)

        refs = check_refs(run, (*result.information_refs, *result.artifact_refs),
                          ref_envelopes(result.structured_output), known, reserved,
                          require_known=False)
        run.ok(VerificationStage.V3_REFS)

        if accounting.violations:
            run.reject(VerificationStage.V4_ACCOUNTING, "RECONCILIATION_VIOLATION",
                       "; ".join(accounting.violations))
        run.ok(VerificationStage.V4_ACCOUNTING)

        try:
            derived = self.ingestor.derive_deltas(result)
            derived = tuple(derived)
        except Exception as exc:  # an ingestor that cannot decide rejects
            run.reject(VerificationStage.V5_INGESTION_STRUCTURE, "INGESTOR_ERROR", str(exc))
        if any(not isinstance(delta, StateDelta) or not isinstance(delta.operation, str)
               or not delta.operation or not isinstance(delta.target_ref_or_path, str)
               for delta in derived):
            run.reject(VerificationStage.V5_INGESTION_STRUCTURE, "MALFORMED_DELTA",
                       "ingestion must derive well-formed StateDeltas")
        if any(delta.reason_ref is not None and (
                not _is_ref(delta.reason_ref) or delta.reason_ref.visibility != Visibility.AGENT)
               for delta in derived):
            run.reject(VerificationStage.V5_INGESTION_STRUCTURE, "HIDDEN_REASON_REF",
                       "ingestion delta cites a malformed or non-AGENT reason ref")
        # Ingestion binds the CURRENT revision, never the originating projection's.
        deltas = tuple(replace(delta, producer=INGESTION_PRODUCER,
                               proposed_base_revision=current_revision) for delta in derived)
        run.ok(VerificationStage.V5_INGESTION_STRUCTURE)

        try:
            verdict = self.verifier.verify_result(result, request, spec, deltas, current_revision)
        except Exception as exc:  # a verifier that cannot decide rejects
            run.reject(VerificationStage.CONSUMER, "VERIFIER_ERROR", str(exc))
        if verdict is not True:
            run.reject(VerificationStage.CONSUMER, "CONSUMER_REJECTED",
                       "consumer verify_result did not return True")
        run.ok(VerificationStage.CONSUMER)

        if revision_reader() != current_revision:
            run.reject(VerificationStage.V6_REVISION, "STATE_CHANGED",
                       "task state changed during verification")
        run.ok(VerificationStage.V6_REVISION)
        return VerifiedResult(run.accept("result verified for deterministic ingestion"),
                              deltas, refs)

    def verify_finish(self, proposal: FinishProposal, task: Task,
                      known: Mapping[str, InformationRef], current_revision: Revision
                      ) -> VerificationDecision:
        """Structural finish checks; consumer ``verify_finish`` runs afterwards."""
        run = _Run(task.task_id, current_revision)
        problems = schema_errors(task.output_schema) or instance_errors(
            proposal.structured_output, task.output_schema)
        if problems:
            run.reject(VerificationStage.FINISH, "INVALID_FINAL_OUTPUT", "; ".join(problems))
        check_refs(run, (*proposal.information_refs, *proposal.artifact_refs),
                   ref_envelopes(proposal.structured_output), known, known, require_known=True)
        run.ok(VerificationStage.FINISH)
        return run.accept("finish structure verified")
