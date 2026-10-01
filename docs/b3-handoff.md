# B3 post-execution verification handoff

Branch `feat/result-verification-v0`. Owning spec:
`docs/specs/hybrid-orchestration-v0.md` (Post-execution Verifier, Deterministic
result ingestion; see "B3 implementation notes").

## Delivered

- `verification.py`: `ResultVerificationPipeline`, `VerificationDecision`,
  `VerificationRejected`, `VerificationStage`, `VerifiedResult`.
- Coordinator: every result passes the pipeline before ingestion; structured
  `VERIFY_RESULT` / `VERIFY_FINISH` / `INGESTION_ORDER` / `RESULT_INGESTION` trace.
- Tests: `tests/test_verification.py` (21 tests) plus the unchanged B1/B2 suites.

## Requirement mapping

| Requirement | Mechanism / test |
|---|---|
| result/request identity | `V0_IDENTITY`; `test_misbound_result...`, `test_untyped_result...` |
| output schema | `V2_OUTPUT_SCHEMA`; `test_output_schema_violation_is_rejected` |
| ref structure/existence | `V3_REFS` + consumer `verify_result`; ref tests |
| provenance / tool version | `V1_PROVENANCE`; `test_missing_tool_version...` |
| visibility / hidden truth | `HIDDEN_REF`, `REF_ID_CONFLICT`, `HIDDEN_REF_IN_OUTPUT` |
| accounting vs B2 | `V4_ACCOUNTING`; `test_overdraw_is_a_structured_verification_failure` |
| consumer invariants | `CONSUMER`; `test_consumer_verdict_must_be_exactly_true` |
| structure before ingestion | `V5_INGESTION_STRUCTURE`, `V6_REVISION` |
| failure blocks ingestion and citation | `test_rejected_result_ref_is_never_citable_later` |
| current-revision binding | `test_ingestion_binds_current_revision_not_projection_revision` |
| trace-visible stable order | `test_wave_ingestion_order_is_trace_visible_and_stable` |
| determinism | `test_identical_runs_produce_identical_verification_traces` |

## Contract gaps resolved conservatively (flagged for review, not blocking)

1. `ResultVerifier.verify_result` returns `bool`, so a consumer rejection is traced
   as `CONSUMER/CONSUMER_REJECTED` without a consumer-specific reason code. Runtime
   stages are fully structured.
2. The spec requires "provenance/tool-version fields" without naming them. v0
   requires a nonempty `provenance.tool_version`; optional `request_id`/`tool_name`
   must match the dispatched request when present.
3. Ref content existence remains in consumer `verify_result` (no new resolver hook);
   the runtime checks structure, visibility, id conflicts, embedded refs, and that
   finish refs are verified refs of the run.
4. Finish output is validated against `Task.output_schema` with the B2 schema
   subset; unsupported keywords fail closed.

No `SPEC_CONFLICT`.
