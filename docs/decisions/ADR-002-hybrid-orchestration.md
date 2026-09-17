# ADR-002 — Hybrid orchestration for agent investigations

Status: accepted  
Date: 2026-09-15

## Context

The target industrial Agent must be autonomous enough to choose evidence, hypotheses, experiments, and delegation, while process invariants, budgets, authority, state transitions, and execution semantics should not consume repeated LLM reasoning.

Pure ReAct is flexible but can create long/unstructured trajectories. A fully fixed workflow is reproducible but cannot adapt strategy to unknown incidents. A fully model-controlled Dynamic DAG adds flexibility but creates unnecessary v0 execution semantics before its value is demonstrated.

The design review therefore retained a Hybrid control/authority boundary while simplifying v0 dynamic planning to dependency-aware WorkBatch.

## Decision

Use a Hybrid runtime:

```text
ContextProjection
        |
Main Agent / ModelTurn
        |
        +-- optional ModelStateUpdateProposal
        |      -> validated atomic TaskStateStore.apply_batch
        |
        +-- one action
               |
      TOOL_REQUEST | WORK_BATCH | FINISH | NONE
               |
       pre-execution deterministic gates
               |
            Executor
               |
       post-execution verify_result
               |
     deterministic result ingestion
               |
        TaskStateStore.apply_batch
```

### Authority

- Main Agent decides strategy and proposes work/internal reasoning-state changes.
- Model-proposed state changes are explicit, revision-bound, and limited to consumer task state.
- Coordinator owns runtime routing, budgets, WorkBatch validation/scheduling, and hard termination.
- Executor performs the exact validated executable request.
- Post-execution Verifier checks machine-checkable result/provenance/invariant conditions.
- Domain validators/state-operation semantics remain consumer-owned.

Coordinator/Executor/Verifier are not permanent LLM agents.

## Framework decision

Public runtime contracts remain framework-neutral and serializable.

LangGraph is **not** a v0 dependency or scheduled deliverable. Reconsider only when a concrete checkpoint/resume/interrupt requirement exceeds the reference loop.

MCP is likewise not a runtime dependency; it may later be one external Tool Provider protocol behind normal tool registration/gates.

## Work planning decision

v0 uses:

```text
WorkBatch
  WorkItem(kind=TOOL | SUBTASK, depends_on=[...])
```

Coordinator validates acyclicity, dependencies, budgets, authority, and visibility before scheduling.

Simple work must not require a WorkBatch.

A richer mutable Dynamic DAG with replanning/cancellation/persistent graph semantics remains an empirical research extension, not v0 infrastructure.

## State-update decision

`ModelTurn` may contain an optional `ModelStateUpdateProposal` plus one action.

Model state updates:

- are bound to the exact ContextProjection revision seen by the model;
- are atomically validated/applied by consumer TaskStateStore;
- consume step budget, not tool-call budget;
- cannot alter generic runtime authority/budget/status or external/reference world;
- block same-turn executable dispatch if rejected.

Deterministic result-ingestion deltas bind the then-current revision at application time so parallel WorkBatch results do not false-fail as stale.

## Consequences

Positive:

- preserves autonomy for investigation/experiment planning;
- externalizes reasoning state instead of relying on conversation memory;
- keeps execution/state authority auditable;
- supports bounded parallel work without a large graph engine;
- supports direct comparison of one-shot/ReAct/fixed workflow/Hybrid/WorkBatch/subagent architectures;
- avoids generic-runtime coupling to TEP or one framework.

Costs:

- more typed state contracts than a plain ReAct loop;
- consumer TaskStateStore must define legal domain update operations;
- parallel result ingestion needs deterministic ordering;
- two mutation domains must remain distinct: internal task state versus external/reference-world mutation.

## Alternatives

1. Pure ReAct — retained as an ablation/local reasoning pattern, rejected as the sole authority/state architecture.
2. Fixed workflow only — retained as an ablation, rejected as the only investigation strategy.
3. Full Dynamic DAG v0 — deferred because its value is not yet demonstrated and its node/replan/cancel semantics would add premature complexity.
4. Permanent Coordinator/Executor/Verifier agents — rejected; these responsibilities are deterministic components unless a bounded semantic critic is explicitly studied.
5. Tool calls for all reasoning-state bookkeeping — rejected; hypothesis/evidence/working-state updates use the explicit ModelStateUpdateProposal path and do not inflate tool-call accounting.

## Revisit trigger

Revisit if benchmark evidence shows a simpler architecture achieves equal/better investigation quality/efficiency, if WorkBatch is insufficient for measured complex cases, or if another domain demonstrates the state-update/action split is too restrictive.
