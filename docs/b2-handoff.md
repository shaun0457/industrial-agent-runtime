# B2 handoff — deterministic pre-execution gates

Branch: `feat/deterministic-gates-v0`. Base: `main` `eda061d`.
Owning spec: `docs/specs/deterministic-gates-v0.md` (implementation notes appended).

## Delivered

- `gates.py`: `GatePipeline` (G0 schema/ref → G1 allowlist/authority → G2
  reservation → G3 side-effect → consumer `validate_request` → approval →
  `FrozenRequest`), `GatePolicy` with monotonic delegation, `reconcile`, and the
  `ReferenceStateGuard`/`ApprovalHook` protocols;
- `schema.py`: fail-closed JSON Schema subset;
- `coordinator.py`: B1 READ/COMPUTE-only restrictions replaced by the pipeline,
  dispatch-time rebinding, reservation/reconciliation accounting, WorkBatch
  cumulative reservation preflight, reference-mutation invariant, and result refs
  becoming citable after verified ingestion. Every stage decision is traced as a
  `GATE` event, with a `RECONCILIATION` event per execution.

## Verification

```text
PYTHONPATH=src python -m unittest discover -s tests
```

92 tests passed locally on Python 3.13.0 and 3.12.4 (62 B1 regression + 30 B2).
All 12 spec acceptance tests are covered, along with negative paths. CI runs 3.11
and 3.13. No domain, LangGraph, or MCP import.

## Not implemented (out of B2 scope)

B3 output-schema/ref/provenance verification beyond reconciliation, B4 SUBTASK
execution, token metering, parallel executor, a UI or provider approval surface.

## Contract gaps — adjudicated at Batch-2 review closure

The three gaps flagged earlier are now accepted decisions, recorded in the spec
("Accepted Batch-2 decisions") and the program Decision Register:

1. D-036: `GatePolicy` stays separate from `Task`; effective authority is their
   intersection; child policies only narrow / tighten approval and inherit
   `simulation_dimensions` unchanged (now enforced).
2. D-037: no expression language; dynamic draws reserve `max_budget_draw`, missing
   maximum is `UNRESERVABLE_DRAW`, actual usage is reconciled.
3. D-038: task-state and reference-world revisions are separate fields on
   `FrozenRequest`; MUTATE binds and re-checks both before dispatch.
