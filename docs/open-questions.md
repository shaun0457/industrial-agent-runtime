# Open Questions — `industrial-agent-runtime`

Only unresolved empirical/implementation choices belong here. The v0 control/state boundary has passed Design Freeze.

## OQ-1 — First real model provider

Core remains provider-agnostic and fake-provider-testable.

Choose the first real adapter based on:

- structured output/tool reliability;
- investigation/planning quality;
- tracing/token/cost visibility;
- reproducible configuration controls;
- CI/local testing practicality.

Provider choice is an adapter/experiment decision, not architecture.

## OQ-2 — Subagent depth/limit

Initial proposal remains depth 1 and cumulative max 3 children per task. Change only from measured quality/context/cost results.

## OQ-3 — Parallel executor implementation

The semantic contract allows independent WorkBatch items to run in parallel. The first implementation may be sequential for correctness.

Open implementation choice: asyncio, threads, processes, or job abstraction depending on provider/tool blocking behavior.

Public contracts must not depend on the choice.

## OQ-4 — Persistent checkpoint backend

No checkpoint framework/backend is required in v0.

Revisit only after concrete resume/interrupt/long-run requirements exist. Possible later backends include in-memory/SQLite/Postgres/object storage or an external orchestration adapter.

Do not select infrastructure before measured need.

## OQ-5 — Authority-escalation interface

Need a provider/UI-neutral contract only when a task enables high-authority MUTATE.

The surface may live in CLI/UI/API; core only needs frozen request + expected-state revision + approval/denial transition.

Blind RCA does not depend on it.

## OQ-6 — Optional semantic critic subtask

Deterministic Verifier is authoritative for machine-checkable constraints.

Open research question: does an optional LLM critic subtask improve semantic completeness enough to justify cost? It returns advice/evidence only and cannot override deterministic verification.

## OQ-7 — Cross-run learned memory

No persistent learned Agent memory in v0. Revisit only after repeated-task studies show measurable value beyond archived Engineering Records/rules/run artifacts.

## OQ-8 — Semantic stop policy

v0 contract is fixed:

```text
Main Agent FinishProposal
 -> deterministic structural readiness checks
```

Open research: whether a formal belief/information-value stopping policy improves efficiency and how its probabilities/thresholds should be defined.

## OQ-9 — Rich Dynamic DAG extension

v0 uses dependency-aware WorkBatch.

Only after O4/O5 experiments show need should the program specify richer capabilities such as:

- graph revision/replanning;
- cancellation;
- persistent graph execution;
- richer dynamic parallelism.

Do not implement these semantics speculatively.

## OQ-10 — LangGraph / MCP extension thresholds

Neither is a v0 dependency.

- Consider LangGraph only when checkpoint/resume/interrupt requirements materially exceed the simple reference loop.
- Consider MCP only when an external tool/service benefits from that provider protocol.

Neither becomes authorization authority merely because it is adopted.
