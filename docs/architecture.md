# Architecture — Industrial Agent Runtime

## Purpose

Provide a domain-independent **control plane** for goal-driven Agent work while keeping domain state/semantics in consumers and execution authority in deterministic code.

## Reference architecture

```text
Task + InformationRef(s)
        |
        v
consumer TaskStateStore.project()
        |
        v
ContextProjection
        |
        v
Main Agent / ModelTurn
        |
        +-- ModelStateUpdateProposal? -------------------+
        |                                                |
        |                                    TaskStateStore.apply_batch
        |                                                |
        +-- action: NONE | TOOL | WORK_BATCH | FINISH ---+
                         |
                         v
               pre-execution G0-G3
               + consumer.validate_request
                         |
                         v
                 Deterministic Executor
                         |
                         v
               post-execution verify_result
                         |
                         v
              deterministic result ingestion
                         |
                         v
              TaskStateStore.apply_batch
                         |
                         +----> project / next Main Agent turn / finish
```

Only the Main Agent is assumed to require an LLM.

Coordinator/Executor/Verifier are module responsibilities, not independent services or permanent LLM personas.

## Main Agent

Open-ended work:

- reason over the current ContextProjection;
- propose internal typed investigation-state changes;
- choose useful tools;
- propose dependency-aware work/subtasks;
- integrate returned results;
- propose final structured output;
- provide semantic judgment that evidence appears sufficient.

The model never grants its own execution authority and never calls TaskStateStore directly.

## ModelTurn / state-update semantics

Canonical shape is owned by `specs/runtime-v0.md`:

```text
ModelTurn
  context_projection_ref
  base_revision
  state_update?: ModelStateUpdateProposal
  action: NONE | TOOL_REQUEST | WORK_BATCH | FINISH_PROPOSAL
```

A model-proposed state-update batch:

- is bound to the exact ContextProjection revision seen by the model;
- is atomically validated/applied through consumer `TaskStateStore.apply_batch`;
- consumes one `max_steps` and zero `max_tool_calls`;
- may change only allowlisted consumer task-state fields/objects;
- cannot change runtime budget/policy/generic status/authority or external/reference state;
- prevents the same turn's executable action from dispatching if stale/invalid.

## Coordinator

Generic deterministic loop/control responsibilities:

- validate ModelTurn routing/base revision;
- apply valid model-proposed state-update batches;
- route Main Agent actions;
- track generic TaskStatus/budget;
- validate WorkBatch dependency structure;
- schedule ready work;
- enforce cumulative child/resource limits;
- freeze validated requests before dispatch;
- call consumer TaskStateStore/projection interfaces;
- serialize deterministic result ingestion;
- enforce hard runtime termination.

Coordinator does not understand TEP/RCA fields.

## Pre-execution gates

Executable work only:

```text
G0 schema/parse
G1 allowlist/authority
G2 budget/resource reservation/recursion
G3 side-effect policy
consumer.validate_request
optional approval/escalation
```

Internal ModelStateUpdateProposal processing is a separate TaskStateStore path and is not a fake tool call.

## Executor

Small deterministic dispatch responsibility:

- execute the exact frozen request;
- dispatch tool/subtask adapter;
- enforce timeout/resource caps;
- bind request/result refs;
- collect actual resource usage/provenance;
- never re-plan/reinterpret intent.

## Post-execution Verifier

Mechanically verifies results/state-ingestion inputs that only exist after dispatch:

- output/ref/artifact existence;
- required provenance/version fields;
- actual versus reserved budget accounting;
- WorkBatch dependency completion;
- visibility/ground-truth isolation metadata;
- consumer-supplied result invariants;
- structural final-output readiness.

Open-ended critique is an optional ordinary subtask, not deterministic authority.

## Deterministic result ingestion

Successful verified results may produce consumer-owned StateDelta batches.

Generic rule:

- model-proposed deltas remain bound to the projection revision the model saw;
- deterministic tool/result-derived deltas bind the **current** task-state revision immediately before apply;
- parallel WorkBatch result-ingestion batches are applied in a deterministic stable work-item order;
- each ingestion batch is atomic and trace-visible.

This prevents multiple results originating from the same old projection from false-failing stale revision checks.

## Public contracts

Canonical schemas are owned by `specs/runtime-v0.md` and related specs.

Important contracts:

```text
InformationRef
Task
Budget + extra_dimensions
ToolSpec + declared/max budget draw
ToolCallRequest / ToolResult
TaskStatus
StateDelta / ModelStateUpdateProposal / ModelTurn
ContextProjection
TaskStateStore.apply_batch
WorkBatch / WorkItem
Subtask / SubtaskResult
RuntimeResult
TraceEvent
```

## Dependency-aware work

v0 supports:

```text
WorkBatch
  WorkItem(kind=TOOL | SUBTASK, depends_on=[...])
```

- acyclic dependency graph;
- ready items may execute in parallel within budget;
- failed required dependency => dependent skipped;
- no silent retries;
- merge/open-ended reasoning = next Main Agent turn.

Simulation is a TOOL whose ToolSpec class is SIMULATE.

A richer mutable Dynamic DAG engine is intentionally not a v0 core component.

## State boundary

Runtime owns only generic task-state interfaces/status/revision envelopes.

Consumers implement application state, legal operations, deterministic result-ingestion mapping, and ContextProjection selection.

Example:

```text
industrial-agent-runtime knows TaskStateStore
tep-agent-lab implements it with RcaState
```

No runtime import of consumer state classes is allowed.

## Context boundary

Consumer constructs domain-relevant ContextProjection.

Runtime validates generic:

- InformationRef integrity;
- visibility metadata;
- token/size policy;
- exact projection persistence/checksum.

There is no generic domain-relevance ContextBroker in v0.

## Tool/budget boundary

Tool side-effect classes:

```text
READ
COMPUTE
SIMULATE
PROPOSE
MUTATE
ADMIN
```

Authority only narrows through delegation; there is no implicit host exception that grants children stronger authority.

Budget supports named extra dimensions so consumers can govern simulator rollouts/horizon/optimizer trials without teaching runtime their domain meaning.

Compound tools declare/reserve nested resource draw before dispatch.

## Tracing

Every significant transition records lineage/resource/status/provenance.

Every model turn MUST reference the exact immutable ContextProjection plus prompt/model/tool metadata. State-update proposal disposition/resulting revision is also trace-visible. Input/output summaries alone are insufficient for replay/leakage audit.

## Framework boundary

Public contracts remain serializable/framework-neutral.

LangGraph is not a v0 dependency or scheduled deliverable. It may be considered later for concrete durable checkpoint/resume/interrupt needs.

MCP is not runtime architecture; it may later provide external tools behind the ordinary ToolSpec/adapter/gate path.

## Memory

v0 has durable per-run records/traces but no automatic learned cross-run memory.

Historical Engineering Records/retrieval remain consumer capability experiments, not core runtime behavior.

## Domain boundary

Core runtime contains no TEP variables/equations, DEXPI parsing, HAZOP mappings, process rules, benchmark fixtures, scientific Tool Bridge code, or domain safety thresholds.

## Canonical specs

- `specs/runtime-v0.md`
- `specs/hybrid-orchestration-v0.md`
- `specs/deterministic-gates-v0.md`
- `specs/subagents-v0.md`
