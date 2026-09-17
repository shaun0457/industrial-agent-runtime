# Ephemeral Subagents v0

Status: proposal  
Version: v0  
Owner repo: `industrial-agent-runtime`

## Goal

Allow the Main Agent to delegate narrow independent reasoning tasks without reintroducing a fixed Supervisor/MachineExpert/DataScientist organization or unbounded multi-agent chatter.

A subagent is a temporary child task, not a persistent persona or organizational role.

## `Subtask`

```text
subtask_id
parent_task_id
goal
context_refs[]
allowed_tools[]
budget
output_schema
reason_for_delegation
```

## `SubtaskResult`

A child returns a compact structured result, not an `EvidenceBundle` and not its hidden transcript.

```text
SubtaskResult
  subtask_id
  status
  claim_or_result
  observation_refs[]
  artifact_refs[]
  uncertainty_or_confidence?
  warnings[]
  budget_usage
  trace_ref
```

The consuming application/parent may later attach referenced observations as evidence for a hypothesis. Runtime does not declare child output to be privileged evidence merely because it came from a subagent.

## v0 policy defaults

```text
max_subagent_depth = 1
max_subagents = 3   # cumulative per task/run
child_mutate_authority = disabled
child_can_spawn_subagents = false
```

These are proposed experiment defaults, not universal constants.

`max_subagents` is cumulative across direct spawns and all `SUBTASK` WorkItems in all WorkBatches. Creating a new batch/replan does not reset the count.

## When spawning is justified

A subtask may be useful when at least one is true:

- independent hypotheses can be tested in parallel;
- a narrow context substantially reduces parent context pressure;
- independent critique has a precise output contract;
- separate isolated simulation experiments can run safely;
- a task can be delegated without requiring child-to-child free-form coordination.

Do not spawn merely because a specialist job title can be invented.

## Authority model

A child may use only tools/side-effect classes explicitly delegated within the intersection of task and parent authority.

Possible v0 classes:

- READ — allowed if delegated;
- COMPUTE — allowed if delegated;
- SIMULATE — allowed if delegated and isolation policy passes;
- PROPOSE — may be delegated because it creates candidate data only;
- MUTATE — disabled for children in v0;
- ADMIN — disabled.

A PROPOSE result never becomes reference mutation authority.

## Recursion / WorkBatch rule

At depth limit or when `child_can_spawn_subagents=false`:

- a child may request ordinary allowed tools;
- a child may propose a WorkBatch containing TOOL items only if task policy allows batching;
- a child may not propose/execute a WorkBatch containing `SUBTASK` items;
- a child cannot use another runtime path to create an equivalent nested child.

This closes delegation-depth bypass through dependency-aware work planning.

## Context isolation

A child receives only explicitly selected InformationRefs/ContextProjection and permitted ToolSpecs.

It does not automatically inherit:

- full parent transcript;
- all parent tools;
- evaluator-only refs;
- parent hidden scratch/reasoning;
- unrelated artifacts.

## Parallelism

Independent child tasks may run in parallel subject to the parent task's `max_parallel_width`, standard budgets, extra resource dimensions, and consumer isolation policy.

Children must not share mutable reference-world state. Simulation tasks use isolated branches.

## Duplicate-work control

Generic runtime may detect exact/obvious duplicate active/completed subtask IDs/goals and expose child summaries.

Semantic duplicate experiment detection belongs to the consuming application because runtime does not understand Experiment Ledger/domain meaning.

## Failure behavior

A failed child returns a failed `SubtaskResult`.

There is no silent restart or recursive delegation. Parent/Main Agent may explicitly replan within remaining budget.

## Trace requirements

Each spawn records:

```text
parent_task_id
subtask_id
reason_for_delegation
context_projection/ref set
allowed tools
allocated budget
start/end status
actual budget usage
result_ref
```

## Invariants

- Child authority is narrower than or equal to explicit task/parent authority.
- Child cannot escape tool/budget/depth policy.
- Cumulative subagent count includes WorkBatch SUBTASK items.
- No child-to-child free-form chat in v0.
- No child persistence after subtask completion.
- Parent consumes `SubtaskResult`, not full hidden transcript.
- Runtime does not automatically promote child output to evidence/knowledge.

## Acceptance tests

1. Parent spawns two independent read-only subtasks.
2. Children receive different scoped ContextProjections/refs.
3. Child attempting MUTATE is denied before dispatch.
4. Fourth cumulative child is rejected when task limit is three, even across multiple WorkBatches.
5. Child at depth limit cannot submit a WorkBatch containing SUBTASK work.
6. Child may return PROPOSE-class candidate data when explicitly delegated, without mutation.
7. Parent receives compact SubtaskResult and independently attaches relevant observation refs as evidence.
8. Child failure is visible and does not trigger automatic retries.

## Open research

- What measured quality/cost threshold justifies subagent usage?
- Does a model-based critic subtask improve engineering conclusions enough to justify its cost?
- Are deeper delegation trees ever useful for this program?
