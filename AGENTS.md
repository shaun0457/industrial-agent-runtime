# AGENTS.md

Repository-wide rules for coding agents working on `industrial-agent-runtime`.

## Product boundary

This repository is a **domain-independent agent runtime**. Do not encode TEP, chemical-process, manufacturing, finance, or other domain semantics in core runtime code.

## Canonical specs

Before implementation, read:

- `docs/specs/runtime-v0.md`
- `docs/specs/hybrid-orchestration-v0.md`
- `docs/specs/deterministic-gates-v0.md`
- `docs/specs/subagents-v0.md`
- `docs/open-questions.md`
- `docs/decisions/ADR-002-hybrid-orchestration.md`

`ADR-001-minimal-explicit-executor.md` is historical/superseded context, not current architecture authority.

If implementation evidence contradicts the frozen contracts, emit `SPEC_CONFLICT` and update the owning spec/ADR rather than silently inventing semantics.

## Hard rules

1. Prefer deterministic code for validation, permissions, budgeting, serialization, state revisions, routing, dependency scheduling, and machine-checkable verification.
2. Model output is a request/proposal, never execution authority.
3. `ModelTurn` may carry an optional `ModelStateUpdateProposal` plus exactly one action: `NONE | TOOL_REQUEST | WORK_BATCH | FINISH_PROPOSAL`.
4. Model-proposed state updates are bound to the exact `ContextProjection.base_revision` seen by the model and are atomically applied through consumer `TaskStateStore.apply_batch`.
5. A rejected/stale model state-update batch prevents the same turn's execution action from dispatching.
6. Model state-update batches consume `max_steps`, not `max_tool_calls`, and cannot alter runtime budget/policy/generic status/authority or external/reference state.
7. Pre-execution request validation and post-execution result verification are distinct stages.
8. Runtime must not import application/domain state classes; use `TaskStateStore`.
9. Every model turn has an immutable `ContextProjection` ref with prompt/model/tool metadata.
10. Budgets include standard counters plus configured `extra_dimensions`; compound tools cannot hide nested simulator/optimizer use.
11. `SIMULATE` may mutate only isolated/sandbox state and never reference state.
12. `MUTATE` requires deterministic consumer validation and expected-state revision binding when enabled.
13. Subagents are ephemeral child tasks, not persistent roles.
14. Child authority is a strict subset of task/parent authority; no escalation exceptions.
15. Subagent count is cumulative per task, including `SUBTASK` WorkBatch items.
16. Child at depth limit cannot create another SUBTASK through WorkBatch or another equivalent path.
17. Child returns `SubtaskResult`, not `EvidenceBundle`/full transcript.
18. v0 dependency planning is `WorkBatch` with `TOOL | SUBTASK` + `depends_on`; do not invent `ANALYSIS`/`MERGE` node types or a full Dynamic DAG engine.
19. Result-ingestion deltas from parallel WorkBatch items bind the then-current state revision in deterministic stable order; do not reuse one old projection revision for every result.
20. LangGraph and MCP are not v0 core dependencies.
21. Core tests run with a deterministic fake model provider and no domain import.
22. No arbitrary agent shell/Python/import execution as the normal tool model.

## Internal state-update path

```text
ContextProjection
 -> ModelTurn.state_update?
 -> schema / projection-revision validation
 -> consumer TaskStateStore legal-operation/ref/visibility validation
 -> atomic apply_batch
 -> trace disposition
 -> action dispatch only if state update succeeded
```

Do not model this as a fake tool call merely to persist reasoning state.

## Executable request path

```text
ToolCallRequest / WorkItem
 -> G0 schema
 -> G1 allowlist/authority
 -> G2 budget/resource reservation
 -> G3 side-effect policy
 -> consumer.validate_request
 -> optional authority escalation/approval
 -> frozen request
 -> Executor
 -> post-execution verify_result
 -> deterministic result-ingestion StateDelta(s)
 -> TaskStateStore.apply_batch
```

## WorkBatch semantics

```text
WorkItem.kind = TOOL | SUBTASK
```

- ready items may run in parallel within policy;
- failed required dependency => dependent `SKIPPED_DEPENDENCY`;
- no silent retries;
- deterministic result ingestion uses stable work-item order;
- merge/open-ended integration happens on the next Main Agent turn.

## Testing minimum

- fake-provider typed task and every ModelTurn action variant;
- accepted atomic multi-delta model state update;
- stale/illegal state update prevents same-turn action dispatch;
- state-update step/tool budget accounting;
- allowed read tool;
- malformed/unknown request;
- standard/extra-dimensional budget denial;
- compound SIMULATE reservation/accounting;
- consumer request-validator denial;
- post-result missing-ref rejection;
- deterministic parallel result-ingestion revision behavior;
- WorkBatch cycle/dependency/failure behavior;
- subagent cumulative count/depth limit;
- child MUTATE denial;
- exact ContextProjection trace refs;
- deterministic fake-provider replay.

Keep this file concise; detailed semantics belong in `docs/specs`, unresolved empirical questions in `docs/open-questions.md`, and rationale in ADRs.
