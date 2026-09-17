# Roadmap — Industrial Agent Runtime

Phase 0 Design Freeze is complete. Implementation may proceed according to `docs/ecosystem/implementation-plan.md` and the frozen runtime specs.

Canonical specs live in `docs/specs/`.

## Phase 0 — Base contracts

Branch: `feat/contracts-runtime-v0`  
Specs: `runtime-v0.md`, `hybrid-orchestration-v0.md`

Implement:

- `InformationRef`;
- `Task`;
- `Budget` + extra dimensions;
- `ToolSpec` / request/result;
- `TaskStatus` / `StateDelta`;
- `ModelStateUpdateProposal` / `ModelTurn`;
- `ContextProjection` / `TaskStateStore.apply_batch`;
- `WorkBatch` / `WorkItem`;
- `RuntimeResult` / `TraceEvent`;
- model-provider interface;
- deterministic fake provider;
- simple reference execution path.

Exit: fake-provider tests cover typed no-tool/read-tool tasks, model state updates, state-update rejection, and deterministic WorkBatch/result-ingestion behavior.

## Phase 1 — Deterministic reference loop / coordination

Branch: `feat/hybrid-orchestration-v0`  
Spec: `hybrid-orchestration-v0.md`

Implement:

- deterministic Coordinator loop;
- ModelTurn routing;
- atomic model state-update application;
- direct/local Main-Agent routing;
- WorkBatch validation/scheduling;
- deterministic result-ingestion ordering;
- hard stop/termination states.

Exit:

- valid model state update applies before same-turn tool action;
- stale/illegal state update blocks same-turn dispatch;
- simple task avoids WorkBatch overhead;
- valid dependency-aware WorkBatch executes;
- cycle/authority/budget violations fail before execution;
- parallel result ingestion avoids false stale conflicts.

## Phase 2 — Generic deterministic gates

Branch: `feat/deterministic-gates-v0`  
Spec: `deterministic-gates-v0.md`

Implement schema/parse, tool allowlist, budget/resource reservation, side-effect, consumer-validator, and approval-state contracts for executable work.

Internal ModelStateUpdateProposal processing remains a separate TaskStateStore path.

Exit: denied operations provably do not execute and all gate decisions are traceable.

## Phase 3 — Post-execution verification

Implement deterministic result/ref/provenance verification, actual-budget reconciliation, consumer `verify_result`, final-output structural readiness, and deterministic result-ingestion hook integration.

Do not create another agent/service for this responsibility.

## Phase 4 — Ephemeral subagents

Branch: `feat/subagents-v0`  
Spec: `subagents-v0.md`

Implement:

- scoped child task/context/tools;
- `SubtaskResult`;
- parent/child trace;
- configurable depth/count limits;
- no implicit child mutation authority;
- optional independent-item parallel scheduling behind the same WorkBatch contract.

Exit: parent runs bounded child analyses and consumes only structured results/refs.

## Phase 5 — First real model provider

Branch: `feat/provider-adapter-v0`

Choose one provider after fake-provider/core contracts are stable. Keep SDK objects behind the model interface.

Exit: the same smoke task runs under fake and real provider with identical public contract shape/tracing semantics.

## Phase 6 — First domain integration

Consume runtime from `tep-agent-lab`; never import TEP into core.

Required proof:

- RcaState is supplied through consumer TaskStateStore;
- model state updates use generic StateDelta envelope but lab-owned operation semantics;
- TEP tools register through generic ToolSpec;
- lab validators plug into consumer hooks;
- Tool Bridge results look like ordinary typed tools to runtime;
- result ingestion is deterministic/revision-safe;
- core remains domain-free.

## Phase 7 — Performance/concurrency hardening

Only after correctness:

- choose asyncio/thread/process/job execution strategy where needed;
- parallel ready-item scheduling;
- backpressure/timeout handling;
- trace/checkpoint storage backend if needed;
- provider rate-limit handling;
- benchmark runtime overhead.

Do not let concurrency implementation alter public semantic contracts.

## Not in v0 runtime roadmap

- LangGraph adapter unless concrete later need;
- MCP runtime dependency;
- full mutable Dynamic DAG engine;
- global persistent learned memory;
- autonomous hiring/retiring organizations;
- unlimited recursive agents;
- peer-to-peer chat network;
- TEP/process safety rules;
- domain RAG/KG logic;
- arbitrary shell/code execution as default Tool Bridge.
