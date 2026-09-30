# industrial-agent-runtime

Domain-independent control plane for one goal-driven Main Agent using typed tools, explicit typed state updates, deterministic authority gates, consumer-owned state adapters, dependency-aware work, and bounded ephemeral subagents.

## Implemented B1 reference

The `industrial_agent_runtime` Python package implements D-034 typed action
contracts, `FakeProvider`, `Coordinator`, atomic state-update routing, deterministic
TOOL WorkBatch waves and durable projection/state tracing. Public classes are
exported from the package root. See [B1 handoff](docs/b1-handoff.md) for acceptance
coverage and hook obligations.

The coordinator has no default execution authority. Trusted request gate,
Executor, result verifier and ingestion hooks are required for READ/COMPUTE tools.
Other side-effect classes, resource-consuming tools and SUBTASK execution remain
disabled until downstream integration. No external runtime dependencies are needed.

Run the complete offline suite from this repository:

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
py -3.13 -m unittest discover -s tests -v
```

## Purpose

Provide reusable Agent runtime mechanics without embedding TEP, process safety, RCA/HAZOP semantics, or scientific-library implementations.

## Core shape

```text
consumer TaskStateStore.project()
        |
        v
ContextProjection
        |
        v
Main Agent / ModelTurn
        |
        +-- state_update? --> TaskStateStore.apply_batch
        |
        +-- action --------> NONE | ToolCall | WorkBatch | FinishProposal
                                  |
                                  v
                        G0-G3 + consumer.validate_request
                                  |
                                  v
                               Executor
                                  |
                                  v
                        post-execution verify_result
                                  |
                                  v
                     deterministic result ingestion
                                  |
                                  v
                         TaskStateStore.apply_batch
```

Only the Main Agent is assumed to require an LLM.

## Runtime owns

- `InformationRef`;
- `Task` / `Budget` including extra resource dimensions;
- `ToolSpec` / request/result contracts;
- generic `TaskStatus` / `StateDelta` / `ModelStateUpdateProposal` / `ModelTurn`;
- `ContextProjection` / `TaskStateStore.apply_batch` protocol;
- dependency-aware `WorkBatch` (`TOOL | SUBTASK` + `depends_on`);
- deterministic pre-execution gates;
- deterministic Executor/dispatcher;
- deterministic post-execution result verification;
- deterministic result-ingestion ordering;
- ephemeral `Subtask` / `SubtaskResult`;
- provider abstraction + fake provider;
- exact model-turn tracing/resource accounting.

## Runtime does not own

- TEP variables/equations/topology;
- application-specific Investigation/RCA state fields;
- HAZOP/recovery policies;
- domain safety/actuator limits;
- DEXPI parsing;
- scientific Tool Bridge implementations;
- benchmark ground truth/scorers;
- knowledge/promotion workflows.

## Critical invariants

- model proposes; deterministic code authorizes, applies validated internal state updates, and executes external work;
- model state updates are bound to the exact projection revision the model saw;
- rejected/stale state updates block same-turn execution dispatch;
- model state updates consume `max_steps`, not `max_tool_calls`;
- pre-execution validation != post-execution verification;
- runtime never imports domain state types;
- child authority never exceeds task/parent authority;
- SIMULATE never mutates reference state;
- compound tools cannot hide nested budget consumption;
- parallel result-ingestion updates bind current revision in deterministic stable order;
- model turns reference exact immutable ContextProjection artifacts;
- conversation history is not canonical application state;
- no arbitrary Agent Python/shell as the normal tool model.

## Dependency-aware work

v0 deliberately does not implement a general mutable Dynamic DAG engine.

```text
WorkBatch
  items:
    TOOL | SUBTASK
    depends_on[]
```

Coordinator validates acyclicity/budget/authority and schedules ready items. Failed required dependency causes dependents to be skipped. Merge/open-ended integration is the next Main Agent turn.

Full graph replanning/cancellation is later research.

## Framework boundary

Public contracts are serializable/framework-neutral.

- LangGraph: not a v0 dependency; consider only after a concrete checkpoint/resume/interrupt need.
- MCP: not a runtime dependency; may later be one external Tool Provider protocol behind ordinary ToolSpec/gates.

## First consumer

`tep-agent-lab` implements TaskStateStore for RcaState, registers TEP/Tool Bridge adapters, supplies consumer request/result validation, and deterministically maps successful results to domain observation/state deltas.

## Documentation

Canonical contracts:

- `docs/specs/runtime-v0.md`
- `docs/specs/hybrid-orchestration-v0.md`
- `docs/specs/deterministic-gates-v0.md`
- `docs/specs/subagents-v0.md`

Rationale/history:

- `docs/decisions/ADR-002-hybrid-orchestration.md` — current accepted architecture decision
- `docs/decisions/ADR-001-minimal-explicit-executor.md` — superseded historical context

Research/implementation unknowns belong in `docs/open-questions.md`.
