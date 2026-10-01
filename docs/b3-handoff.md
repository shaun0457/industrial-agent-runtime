# B3 post-execution verification handoff

Branch `feat/result-verification-v0`. Owning spec:
`docs/specs/hybrid-orchestration-v0.md` (Post-execution Verifier, Deterministic
result ingestion; see "B3 implementation notes").

## Delivered

- `verification.py`: `ResultVerificationPipeline`, `VerificationDecision`,
  `VerificationRejected`, `VerificationStage`, `VerifiedResult`.
- Coordinator: every result passes the pipeline before ingestion; structured
  `VERIFY_RESULT` / `VERIFY_FINISH` / `INGESTION_ORDER` / `RESULT_INGESTION` trace.
- Tests: `tests/test_verification.py` (29 tests) plus the unchanged B1/B2 suites.

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

## Batch-3 review closure — adjudicated

The earlier contract gaps are accepted decisions D-039–D-042 (see the spec's
"Accepted Batch-3 decisions"): bool consumer verifier retained, minimum generic
provenance (with ToolSpec-declared `tool_version` pinning), content existence
consumer-owned with exact-duplicate ref and `reason_ref` checks, and the shared
B2 schema subset for finish. D-043 (tep-sim ReplaySpec) is recorded alongside.

No `SPEC_CONFLICT`.
