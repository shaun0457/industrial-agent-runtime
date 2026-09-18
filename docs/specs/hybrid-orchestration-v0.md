# Hybrid Orchestration v0

Status: accepted  
Owner repo: `industrial-agent-runtime`  
Primary consumer: `tep-agent-lab`

## Goal

Define a small hybrid runtime that combines:

- one goal-driven Main Agent for open-ended reasoning;
- deterministic coordination, authorization, execution, result verification, budgets, tracing, and state-update application;
- local ReAct-style iteration for open-ended investigation;
- dependency-aware bounded work batches when independent/dependent work can be executed in parallel;
- framework-neutral public contracts.

The architecture is an implementation contract, not a claim that Hybrid orchestration is superior. Its value must be measured against simpler orchestration baselines.

## Core flow

```text
Engineering Goal / Task
          |
          v
+--------------------------+
| Deterministic Coordinator|
+------------+-------------+
             |
             v
+--------------------------+
| Main Agent / ModelTurn   |
| goal-driven reasoning    |
+------+-------------------+
       |
       +-- state_update? --> validate/apply internal task-state deltas
       |
       +-- action ---------> ToolCall / WorkBatch / FinishProposal / None
                                |
                                v
+--------------------------+
| Pre-execution gates      |
| G0-G3 + validate_request |
+------------+-------------+
             |
             v
+--------------------------+
| Deterministic Executor   |
+------------+-------------+
             |
             v
        tools / subtasks
             |
             v
+--------------------------+
| Post-execution Verifier  |
| verify_result            |
+------------+-------------+
             |
             v
+--------------------------+
| deterministic ingestion  |
| + TaskStateStore.apply   |
+------------+-------------+
             |
             +----> next Main Agent turn / finish
```

Only the Main Agent is assumed to require an LLM. Coordinator, gates, Executor, result ingestion, and post-execution Verifier are deterministic runtime/application components unless an experiment explicitly adds a model-based critic as an ordinary bounded subtask.

## Responsibility boundaries

### Main Agent

May:

- interpret the current projected task/domain state;
- propose typed internal task-state updates through `ModelStateUpdateProposal`;
- choose the next useful action;
- request typed tools;
- propose a bounded `WorkBatch`;
- propose bounded subtasks through the WorkBatch/subtask contract;
- integrate returned results;
- propose a final structured result;
- provide semantic judgment that evidence appears sufficient.

It cannot execute tools directly, call `TaskStateStore` directly, change budgets/policies/generic task status, mutate reference-world state directly, or grant itself/children additional authority.

### Coordinator

Generic deterministic control component responsible for:

- requesting a model turn over an immutable `ContextProjection`;
- validating ModelTurn routing shape/base revision;
- applying validated model-proposed state-update batches through `TaskStateStore`;
- routing the action part of a ModelTurn;
- tracking generic `TaskStatus` and budget usage;
- validating `WorkBatch` structure and dependencies;
- scheduling ready work items;
- enforcing cumulative subtask/work-item limits;
- freezing validated requests before dispatch;
- serializing result ingestion/state application deterministically;
- deciding whether hard runtime termination conditions have been reached.

The Coordinator does not understand TEP-specific fields and does not import consumer state types.

### Model state-update path

The state-update path is separate from tool execution authorization.

```text
ModelTurn.state_update
 -> generic schema/projection-base-revision checks
 -> consumer TaskStateStore validates legal operations/refs/visibility
 -> atomic apply_batch
 -> trace disposition
 -> continue to action only if update succeeded
```

Properties:

- one state-update proposal batch consumes one `max_steps` unit and zero `max_tool_calls`;
- all deltas in the model proposal are bound to the projection revision the model actually saw;
- the Agent cannot use this path to change generic budget/policy/task authority or external/reference state;
- if the state-update batch is denied/stale/illegal, the same turn's action is not dispatched.

### Pre-execution gates

Defined in `deterministic-gates-v0.md`.

They validate executable requests before any tool/subtask execution:

```text
G0 schema/parse
G1 tool/operation allowlist
G2 budget/recursion/resource reservation
G3 side-effect policy
consumer.validate_request(...)
optional authority escalation/approval
```

Pre-execution checks must not be called the post-execution Verifier.

### Executor

A deterministic dispatcher responsible for:

- executing the exact frozen request;
- enforcing timeout/resource limits;
- dispatching tool adapters or bounded subtasks;
- binding request/result IDs;
- collecting actual budget/resource usage;
- recording raw result/artifact provenance;
- never broadening authority beyond the frozen request.

The Executor does not re-plan or interpret engineering meaning.

### Post-execution Verifier

A deterministic `verify_result` stage over returned result/provenance and the consumer-proposed deterministic ingestion update.

It may mechanically verify:

- output schema validity;
- artifact/information reference existence;
- declared versus actual budget/resource accounting;
- required provenance/tool-version fields;
- work dependency completion;
- result visibility/ground-truth isolation metadata;
- exact consumer-provided result invariants;
- structural finish requirements.

It does not perform open-ended engineering critique. A model-based critic, when studied, is an ordinary `Subtask` whose output is evidence/advice only.

### Deterministic result ingestion

After a successful verified result, the consuming application may deterministically derive one or more task-state deltas, such as registering an observation/result reference.

For result-ingestion deltas:

- the Coordinator binds the **current** state revision at application time;
- they do not reuse the model projection revision that originally caused the work;
- parallel WorkBatch results are ingested in a deterministic stable order (default: `work_id` lexical/order-defined sequence after all ready results for that scheduling wave are available);
- each ingestion batch is applied atomically through `TaskStateStore.apply_batch`;
- the ordering policy is trace-visible.

This prevents parallel work items originating at revision N from spuriously making all but the first result stale.

## Reference runtime loop

```text
LOAD / PROJECT STATE
 -> MAIN_AGENT_TURN
 -> VALIDATE MODEL TURN
 -> APPLY MODEL STATE_UPDATE?      # internal task state only
      -> reject => feedback / no action dispatch
 -> ROUTE ACTION
      -> NONE
      -> TOOL_REQUEST
      -> WORK_BATCH
      -> FINISH_PROPOSAL
 -> PRE_EXECUTION_GATES            # TOOL/WORK only
 -> EXECUTE_READY_WORK
 -> POST_EXECUTION_VERIFY
 -> DETERMINISTIC RESULT INGESTION
 -> APPLY RESULT STATE_DELTA(S)
 -> HARD_STOP_CHECK
      -> PROJECT / MAIN_AGENT_TURN
      -> FINISH
      -> FAIL / EXHAUSTED / CANCELLED
```

This loop is the v0 semantic reference. It may be implemented with ordinary Python control flow or another framework without changing public contracts.

## Local ReAct behavior

A Main Agent may locally iterate:

```text
projected state
 -> reason
 -> externalize hypothesis/evidence/working-state changes
 -> request one useful tool/work batch
 -> receive verified + ingested results
 -> repeat/finish
```

Conversation history is supplementary trace data, not canonical application state.

## Dependency-aware `WorkBatch`

v0 deliberately does **not** require a general model-revisable Dynamic DAG engine.

A Main Agent may propose:

```text
WorkBatch
  batch_id
  objective
  items[]
  budget_request
  completion_policy: ALL_SETTLED
```

v0 supports exactly one completion policy: `ALL_SETTLED`. The Coordinator runs
dependency-ready work until every item is terminal as `COMPLETED`, `FAILED`, or
`SKIPPED_DEPENDENCY`, deterministically ingests every verified successful result,
and then returns the compact batch outcome to the next Main Agent turn. Existing
dependency-failure propagation and no-silent-retry rules still apply. An unknown
completion policy is rejected before any item executes.

### `WorkItem`

```text
work_id
kind: TOOL | SUBTASK
depends_on[]
request_or_subtask
budget_request
status
```

Simulation is represented as a `TOOL` item whose registered `ToolSpec.side_effect_class` is `SIMULATE`.

There is no v0 `ANALYSIS` node type: deterministic analysis is a TOOL; open-ended analysis is performed by the Main Agent or a SUBTASK.

There is no v0 `MERGE` node type: the next Main Agent turn integrates completed work.

## WorkBatch validation

Before scheduling, the Coordinator MUST check:

- unique item IDs;
- acyclicity of `depends_on`;
- all dependencies exist;
- requested cumulative work/subtask/resource budgets fit the remaining task budget;
- SUBTASK items count against the task's cumulative subagent budget;
- child/subtask authority is a subset of task/parent authority;
- hidden/evaluator-only refs are absent from agent-visible work;
- nested SUBTASK creation is disallowed when depth/policy forbids it.

## Work execution/failure semantics

- Items with all dependencies satisfied may execute in parallel within the configured parallel/resource limit.
- A failed item is recorded as `FAILED`.
- An item depending on a failed required dependency is marked `SKIPPED_DEPENDENCY` and is not executed.
- No item silently retries. Retry requires a new explicit Main Agent request within remaining budget.
- Completed/failed/skipped items remain immutable trace history.
- Verified successful results are ingested deterministically before the next model projection.
- The Main Agent receives compact structured results/failures and may propose a new WorkBatch on a later turn.

## Full Dynamic DAG research boundary

The following are **not required v0 infrastructure**:

- mutable graph revisions in place;
- model-directed cancellation of running branches;
- graph-specific MERGE nodes;
- arbitrary recursive graph generation;
- persistent graph execution across process restarts.

A later orchestration ablation may compare the simple dependency-aware WorkBatch against a richer Dynamic DAG planner/replanner. Richer semantics must be specified before implementation.

## Tool exposure

For comparable orchestration experiments, the same task/capability tool allowlist SHOULD be held fixed across modes.

The runtime may narrow tools for explicit policy, authority, capability, or exhausted-budget reasons. It must not silently change tool exposure merely because an orchestration mode uses a different internal stage unless tool exposure itself is the independent variable being studied.

## Framework boundary

v0 public contracts are framework-neutral and serializable.

LangGraph is not a v0 core dependency. It may be added later if a concrete consumer demonstrates a need for durable checkpoint/resume, human interrupt, or long-running graph persistence that materially exceeds the reference loop.

MCP is not an orchestration dependency; if used later, it is only one possible external Tool Provider protocol behind registered tool adapters.

## Invariants

- Model reasoning proposes; deterministic code authorizes, applies internal state updates, and executes external work.
- Model state updates are explicit/revision-bound; they are not hidden transcript side effects.
- A rejected state update prevents the same turn's execution action from dispatching.
- Pre-execution validation and post-execution result verification are distinct stages.
- Runtime core does not import domain-specific state classes.
- Child authority never exceeds explicit task/parent authority.
- SUBTASK work counts against cumulative task subagent budgets.
- Result-ingestion deltas from parallel work bind the current revision in deterministic ingestion order.
- Important state exists outside conversation transcripts.
- No work item silently retries or mutates prior history.
- Simple tasks do not require a WorkBatch.

## Acceptance tests

1. Simple task completes through one Main Agent/tool loop without a WorkBatch.
2. ModelTurn adds a hypothesis through `state_update` and requests a tool referencing it in the same turn; state update is committed before request validation/dispatch.
3. Stale/illegal model state update is rejected and the same turn's tool/work action does not execute.
4. Main Agent proposes three work items with dependencies; Coordinator validates and schedules ready items deterministically.
5. Cyclic WorkBatch is rejected before execution.
6. SUBTASK item exceeding task subagent budget is rejected before spawn.
7. Child cannot propose/spawn another SUBTASK when depth policy forbids it.
8. Failed work item causes dependent item to become `SKIPPED_DEPENDENCY`, with no silent retry.
9. Two parallel successful TOOL results create deterministic ingestion updates that both apply without stale-revision conflict.
10. Pre-execution consumer `validate_request` denial prevents dispatch.
11. Post-execution `verify_result` rejects a result/final claim referencing a nonexistent artifact/ref.
12. Same ModelTurn/WorkBatch plus deterministic tool results produces identical state-update/scheduling/verification/ingestion trace.
13. Runtime package operates without LangGraph or MCP installed.
