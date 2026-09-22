# Runtime v0

Status: accepted  
Version: v0  
Owner repo: `industrial-agent-runtime`

## Goal

Define the domain-independent contracts used by the Hybrid runtime: one Main Agent, deterministic pre-execution gates, a deterministic dispatcher, post-execution verification, bounded subtasks/work batches, generic state interfaces, explicit model-proposed state updates, extensible budget accounting, and complete trace/provenance without embedding application-specific safety truth.

Detailed orchestration semantics are defined in `hybrid-orchestration-v0.md`.

## Runtime boundary

```text
Task + InformationRef(s)
        |
        v
TaskStateStore.project()
        |
        v
ContextProjection
        |
        v
Main Agent / ModelTurn
        |
        +---- state_update? ----> TaskStateStore.apply_batch()
        |                         (validated internal task state only)
        |
        +---- action -----------> ToolCall / WorkBatch / FinishProposal
                                      |
                                      v
                           G0-G3 + consumer.validate_request
                                      |
                                      v
                                   Executor
                                      |
                                      v
                           consumer.verify_result
                                      |
                                      v
                          result-ingestion StateDelta(s)
                                      |
                                      v
                           TaskStateStore.apply_batch()
```

Runtime core never imports application-specific state, TEP semantics, RCA schemas, or domain safety rules.

## Core contracts

### `InformationRef`

Owned by runtime as the generic cross-component reference envelope; the referenced content remains owned by its producing repository/application.

```text
InformationRef
  ref_id
  kind
  owner
  version
  checksum?
  visibility: AGENT | EVALUATOR | INTERNAL
  created_at
```

A runtime must never place `EVALUATOR` refs into an agent-visible `ContextProjection`.

### `Task`

```text
task_id
goal
context_refs[]
allowed_tools[]
budget
output_schema
parent_task_id?
metadata?
```

Authority is encoded in explicit policy/tool/budget fields, not natural-language prose.

### `Budget`

```text
Budget
  max_model_calls
  max_tool_calls
  max_subagents
  max_subagent_depth
  max_steps
  max_total_tokens?
  max_parallel_width?
  extra_dimensions: map<string, number>
```

`extra_dimensions` lets a consumer add measurable quotas without teaching runtime their domain meaning. Examples:

```text
simulation_rollouts
simulated_horizon_seconds
optimizer_trials
wall_time_seconds
provider_cost_units
```

The runtime treats configured extra dimensions as deterministic counters/reservations.

`max_subagents` is cumulative per task/run, including SUBTASK items proposed through WorkBatch revisions. It is not reset by replanning/new batches.

### `ToolSpec`

```text
ToolSpec
  name
  description
  input_schema
  output_schema
  side_effect_class
  required_policy_tags[]
  declared_budget_draw?: map<string, number | expression>
  max_budget_draw?: map<string, number>
  isolation_guarantee?
  provider_metadata?
```

A compound tool must expose the resource dimensions it can consume. If one call can internally trigger simulator rollouts, the ToolSpec must be `SIMULATE` and declare/reserve the relevant rollout/horizon/trial draw before dispatch.

### `ToolCallRequest`

```text
ToolCallRequest
  request_id: string
  tool_name: string
  arguments: JSON object
```

This is a model-produced typed request. `tool_name` selects one registered
`ToolSpec`; `arguments` is validated against that tool's input schema. The request
contains no execution authority: parse/schema validity never grants permission,
budget, side-effect authority, or dispatch.

### `ToolResult`

```text
ToolResult
  request_id
  status
  structured_output
  artifact_refs[]?
  information_refs[]?
  actual_budget_draw: map<string, number>
  error?
  provenance
```

### `SubtaskResult`

Defined fully in `subagents-v0.md`.

A child returns a compact task result and referenced observations/artifacts, not a privileged evidence object and not its full transcript.

### `WorkBatch`

Dependency-aware work contract is defined in `hybrid-orchestration-v0.md`.

v0 does not require a general mutable Dynamic DAG engine.

### `TaskStatus`

Runtime owns only generic lifecycle status:

```text
RUNNING
WAITING
READY
DONE
FAILED
EXHAUSTED
CANCELLED
```

Domain-specific investigation phases/statuses belong in consumer state/metadata.

### `StateDelta`

Generic envelope for a consumer-owned task-state mutation request:

```text
StateDelta
  operation
  target_ref_or_path
  value_or_ref
  producer
  proposed_base_revision?
  reason_ref?
```

`producer` identifies whether the delta came from a model proposal, deterministic result ingestion, or runtime control logic.

For a **model-proposed** delta, `proposed_base_revision` is REQUIRED and MUST equal the `ContextProjection.base_revision` seen by that model turn. Runtime does not interpret application fields; the consumer `TaskStateStore` validates legal fields/operations and visibility.

For a **result-ingestion/runtime-derived** delta, the Coordinator binds the current task-state revision immediately before application. Such deltas are not rejected merely because several parallel work items originated from the same earlier projection.

### `ModelStateUpdateProposal`

A model may propose internal investigation/task-state changes independently of tool execution:

```text
ModelStateUpdateProposal
  proposal_id
  base_revision
  deltas[]
```

All deltas in one proposal share the same model-visible base revision and are validated/applied atomically as one batch.

Typical consumer-defined operations include adding/updating a hypothesis, linking an observation as evidence, adding an open question, recording an experiment interpretation, or updating a working explanation.

A model state update:

- changes only consumer-owned task/investigation state;
- does not execute a tool or mutate the external/reference world;
- is validated by generic schema/ref checks plus `TaskStateStore` domain validation;
- consumes **zero** `max_tool_calls`;
- consumes **one** `max_steps` unit per accepted/rejected update proposal batch;
- is always traced as an accepted or denied state-update event.

If a model state update is rejected, any execution action from the same `ModelTurn` MUST NOT be dispatched. The next turn receives structured rejection feedback.

### `ModelTurn`

The provider abstraction returns a typed turn rather than unstructured text alone:

```text
ModelTurn
  turn_id
  context_projection_ref
  base_revision
  state_update?: ModelStateUpdateProposal
  action: NONE | TOOL_REQUEST | WORK_BATCH | FINISH_PROPOSAL
  tool_request?
  work_batch?
  finish_proposal?
  prose_summary?
```

Exactly one action variant is active. `state_update` is orthogonal and MAY accompany an action.

Processing order is deterministic:

```text
1. validate ModelTurn schema and projection/base revision
2. validate/apply model state_update atomically, if present
3. if state update failed -> do not dispatch action
4. bind the resulting current task-state revision to the action context
5. route TOOL_REQUEST / WORK_BATCH / FINISH_PROPOSAL / NONE
```

This allows the Agent to externalize a newly formed hypothesis/evidence link before requesting work that references it, without requiring a separate model call.

### `FinishProposal`

```text
FinishProposal
  structured_output: JSON value
  information_refs[]
  artifact_refs[]
```

The proposal is model output, not a status transition. Runtime and consumer
structural readiness checks validate the output schema and every cited ref before
the generic task status may become `READY`/`DONE`. Missing, unknown, hidden, or
inconsistent refs reject the proposal with structured feedback.

### `ContextProjection`

Exact model-visible input must be materialized as an immutable, addressable artifact/ref.

```text
ContextProjection
  projection_id
  task_id
  base_revision
  content
  included_refs[]
  visibility_policy_version
  approximate_tokens
  checksum
```

The consumer chooses relevant domain content. Runtime may enforce visibility, maximum size/token policy, and ref integrity; it does not decide TEP relevance.

### `TaskStateStore`

Consumer-implemented protocol used by the generic Coordinator:

```text
revision() -> Revision
status() -> TaskStatus
project(policy) -> ContextProjection
apply_batch(deltas[], expected_revision) -> NewRevision
transition_status(status, expected_revision) -> NewRevision
```

A consumer MAY expose `apply(delta, expected_revision)` as a convenience wrapper around a single-element batch, but `apply_batch` defines the atomic semantics for model-proposed multi-delta updates.

Required properties:

- optimistic revision checking;
- atomic all-or-nothing validation/application for one batch;
- deterministic rejection of illegal/stale deltas;
- domain field/operation validation remains in consumer implementation;
- runtime depends only on this interface, never the consumer state class.

### Runtime-owned terminal status persistence

`transition_status(status, expected_revision)` is the generic deterministic
Coordinator path for persisting `DONE`, `FAILED`, `EXHAUSTED`, or `CANCELLED`.
It is not a tool or a model-authored StateDelta operation. The model cannot call
this method or grant itself lifecycle authority. Consumer adapters implement the
storage operation; the Coordinator owns the decision to transition.

Required semantics:

- Check the exact expected revision before any mutation. A stale/illegal request
  rejects atomically without changing status, content, or revision.
- From a nonterminal status, atomically persist the requested terminal status and
  a new revision. Return that revision. Domain investigation content is unchanged.
- A request for the already-persisted terminal status is an idempotent no-op and
  returns the unchanged revision after the optimistic revision check.
- A terminal status cannot be replaced by a different terminal status. Reuse or
  restart is a new task/run, not a lifecycle overwrite.
- The Coordinator persists DONE only after a verified FinishProposal; it persists
  EXHAUSTED on hard budget termination and FAILED on runtime failure. An already
  terminal store is observed without making another model call. Cancellation,
  when supplied by a higher-authority caller, follows the same persistence rule.
- After transition, the Coordinator verifies that the returned revision equals
  `revision()` and `status()` equals the requested status. A changed status must
  have a changed revision; an idempotent no-op must retain its revision.
- Trace the requested status, expected revision, accepted/denied disposition and
  observed resulting status/revision. Lifecycle persistence uses no model/tool
  calls or additional step budget, including when work budget is exhausted.
- If persistence fails or postconditions disagree, stop fail-closed: return a
  runtime FAILED result with no successful structured output and an explicit
  `STATUS_PERSISTENCE_FAILED` error. Record the observed store status/revision;
  do not claim a successful durable transition, retry automatically, or overwrite
  another terminal state. The store may remain nonterminal if its transition
  rejected; this discrepancy must remain explicit in trace/result errors.

`RuntimeResult.state_revision` references the observed revision after this path,
including the terminal transition when successful. Generic lifecycle persistence
must not be implemented through a consumer-specific operation name in core.

### `RuntimeResult`

```text
task_id
status
structured_output
state_revision
trace_ref
budget_usage
warnings/errors
```

### `TraceEvent`

Every meaningful transition records:

```text
event_id
task_id
parent_task_id?
batch/work/subtask/request refs?
type
timestamp
status/error
budget delta
latency/cost when known
input_summary
output_summary
artifact_refs[]?
```

For every `MODEL_TURN`, the event MUST additionally reference:

```text
context_projection_ref
prompt_template_version
provider/model/version
sampling/config parameters
registered_tool_set_version or tool-spec refs
```

For every model-proposed state update, trace MUST record proposal ID, projection/base revision, accepted/denied operation names, resulting revision when accepted, and budget step delta.

Summaries are for human display; they are not sufficient evidence for replay/leakage audit.

## Provider abstraction

Core runtime depends on an internal model interface rather than one provider SDK:

```text
generate(context_projection, tool_specs, output_schema, limits) -> ModelTurn
```

A deterministic fake provider is mandatory for tests and MUST be able to emit each `ModelTurn` action variant plus model-proposed state updates.

Provider-specific message objects must not appear in public runtime contracts.

## Main Agent authority

May:

- propose validated internal task-state updates through `ModelStateUpdateProposal`;
- request allowed READ/COMPUTE/SIMULATE tools;
- propose WorkBatch/Subtask work;
- consume `SubtaskResult`s;
- propose final typed output;
- create PROPOSE-class candidate data when explicitly allowlisted.

Cannot:

- bypass gates/consumer validation;
- execute adapters directly;
- call `TaskStateStore` directly;
- change generic task status/budget/policy through a model state delta;
- grant child authority;
- directly mutate reference-world state.

## Model state-update path versus tool path

A model-proposed state update is **not** a tool call. It modifies only the consumer's investigation/task state through the validated `TaskStateStore` contract.

```text
ModelStateUpdateProposal
 -> schema / projection-base-revision checks
 -> consumer TaskStateStore validates legal operations + refs + visibility
 -> atomic apply_batch
 -> trace + max_steps accounting
```

A tool/work action follows the separate pre-execution authorization path below.

This distinction prevents internal reasoning-state bookkeeping from inflating `max_tool_calls` while still making every model-authored state mutation explicit, validated, revision-bound, and auditable.

## Pre/post validation split

### Pre-execution

`deterministic-gates-v0.md` owns tool/work execution authorization:

```text
G0 parse/schema
G1 allowlist
G2 budget/resource reservation/recursion
G3 side-effect policy
consumer.validate_request
optional approval/escalation
```

### Post-execution

`hybrid-orchestration-v0.md` owns deterministic `verify_result` checks over returned data/provenance and result-ingestion state updates.

Do not use one ambiguous "Verifier" term for both phases.

## Failure behavior

Fail closed for malformed output, stale/rejected model state update, unknown tools, exhausted/unreservable budget, invalid WorkBatch/subtask request, denied authority, missing refs, hidden-ref exposure, or consumer request/result validation failure.

A denial/failure is a structured trace event. Replanning/retry requires a new explicit Main Agent decision within remaining budget.

## Context discipline

Consumers resolve domain refs and construct `ContextProjection`; runtime enforces generic visibility/ref/size policy.

Child transcripts are not copied into parent context. Conversation history is not canonical task state.

## Persistence

v0 requires:

- durable per-run trace/events;
- immutable ContextProjection artifacts for model turns;
- serializable task/state references sufficient for audit/replay;
- persisted model-proposed and result-derived state-update events.

It does not require learned cross-run Agent memory.

## Framework boundary

Runtime contracts are serializable and framework-neutral.

LangGraph is not required by v0 and is not on the critical implementation path. Add an adapter only after a concrete consumer demonstrates checkpoint/resume/interrupt/persistent-graph requirements not reasonably served by the reference runtime loop.

MCP is not a runtime dependency. A future adapter may register MCP-provided tools through the same ToolSpec/gate/Executor contract.

## Invariants

- No domain-specific variables/rules/safety truth in runtime core.
- Runtime does not import consumer state types.
- Model-authored internal state changes are explicit `ModelStateUpdateProposal`s, never hidden transcript effects.
- Model-proposed state updates are projection-revision-bound and atomically validated/applied.
- Tool/result-derived updates are rebound to the current revision during deterministic ingestion.
- Pre-execution authorization and post-execution result verification are distinct.
- Every tool/WorkBatch/subtask is validated before dispatch.
- Child authority never exceeds explicit parent/task authority.
- Compound tools cannot hide resource use from configured budget dimensions.
- Every model turn has an exact immutable context projection ref.
- Every task terminates by success, explicit failure, cancellation, or exhaustion.
- Core tests run without network/provider access using the fake provider.

## Acceptance tests

1. Fake provider completes a typed no-tool task.
2. Fake provider emits a model state update that adds a consumer-defined hypothesis and then requests a tool referencing it in the same turn.
3. Stale model-proposed state update is rejected and the accompanying action is not dispatched.
4. Atomic multi-delta proposal either applies all legal deltas in one revision transition or applies none.
5. State-update proposal consumes one `max_steps` unit and zero `max_tool_calls`.
6. Unknown/denied tool is rejected without dispatch.
7. Standard and extra-dimension budget exhaustion deterministically stops/denies execution.
8. A SIMULATE compound tool reserves rollout/trial budget before adapter execution and reconciles actual usage after.
9. Consumer `TaskStateStore` implementation can project/apply state without runtime importing consumer types.
10. Parallel WorkBatch result-ingestion deltas are applied in deterministic work-item order against the then-current revision rather than all reusing the originating projection revision.
11. Model turn trace includes immutable ContextProjection ref, prompt/model/tool metadata, and any state-update disposition.
12. Parent spawns one valid child and receives only `SubtaskResult`.
13. Post-execution verifier rejects a final result referencing a nonexistent artifact/ref.
14. Core package imports no TEP/domain, LangGraph, or MCP requirement.
15. Verified finish, hard budget exhaustion, and runtime failure persist DONE,
    EXHAUSTED, and FAILED respectively through `transition_status`; result revision
    includes that atomic transition.
16. An already terminal store is observed without a provider call; same-terminal
    transition is idempotent, stale or different-terminal transitions reject.
17. A transition exception or inconsistent returned revision/store status produces
    runtime FAILED with cleared output and explicit STATUS_PERSISTENCE_FAILED
    trace/result evidence, without retrying or claiming persistence succeeded.

## Non-goals

v0 does not implement global learned memory, fixed specialist organizations, unrestricted peer-to-peer chat, arbitrary recursive swarms, application-specific safety truth, a general mutable Dynamic DAG engine, or arbitrary agent shell/Python execution as the normal tool model.
