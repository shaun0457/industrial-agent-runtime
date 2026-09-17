# Deterministic Gates v0

Status: accepted  
Version: v0  
Owner repo: `industrial-agent-runtime`

## Goal

Separate model reasoning from execution authority through deterministic, auditable **pre-execution** gates. Post-execution result verification is a separate contract in `hybrid-orchestration-v0.md`.

Core runtime gates remain generic. Domain-specific process/safety policy is supplied by consumer-owned validators.

## Scope distinction

This spec governs **executable ToolCall/WorkItem requests**.

A model-proposed internal task-state update (`ModelStateUpdateProposal`) is not a tool call and does not enter G1-G3. Its separate path is defined in `runtime-v0.md`:

```text
ModelStateUpdateProposal
 -> schema/projection revision checks
 -> consumer TaskStateStore legal-operation/ref/visibility validation
 -> atomic apply_batch
```

That path may mutate only consumer-owned investigation/task state. It cannot execute external tools, change runtime budget/policy/authority, or mutate reference-world state.

## Pre-execution gate pipeline

```text
Model ToolCallRequest / WorkItem
        |
        v
G0 Parse / schema gate
        |
        v
G1 Tool/operation allowlist gate
        |
        v
G2 Budget / recursion / resource-reservation gate
        |
        v
G3 Side-effect policy gate
        |
        v
consumer.validate_request(...)
        |
        v
optional authority escalation / approval
        |
        v
freeze exact request + expected revision
        |
        v
Executor dispatch
```

A rejection at any stage prevents execution and produces a structured `GateDecision` trace event.

## Tool side-effect classes

```text
READ       - inspect external state, no mutation
COMPUTE    - deterministic/local transformation, no external/reference mutation
SIMULATE   - isolated/sandboxed execution that may mutate only a branch/sandbox, never reference state
PROPOSE    - create candidate change data only; no reference mutation
MUTATE     - change reference/external state
ADMIN      - change runtime/policy/configuration or high-authority external state
```

A tool that internally executes simulator rollouts is `SIMULATE` even when its top-level purpose is optimization, sensitivity analysis, or statistical experiment design.

Consumers may add metadata/tags but must preserve monotonic authority. A child/task cannot upgrade itself to a stronger side-effect class.

## `GateDecision`

```text
request_id
decision: ALLOW | DENY | REQUIRE_APPROVAL
stage
reason_code
reason
policy_version
validator_refs[]?
reserved_budget_draw?
expected_state_revision?
normalized_request_ref?
```

A model-authored rationale is never a GateDecision.

## G0 — schema gate

Checks:

- operation/tool identity;
- input schema;
- required identifiers;
- parse validity;
- declared output expectations.

No side effect occurs before G0 passes.

ModelTurn/StateUpdate schema checks use the same generic parsing discipline but are routed by `runtime-v0.md`, not treated as executable ToolSpecs.

## G1 — allowlist/authority gate

Checks that the current task/agent/subtask has explicit authority for the operation/tool/class.

Absence means deny.

Delegation is monotonic: child authority is always a subset of task/parent authority. There is no host-policy exception that silently grants a child authority its parent did not delegate.

## G2 — budget / recursion / resource reservation

Checks standard budgets and configured `Budget.extra_dimensions`.

Before dispatch, runtime reserves the maximum/declared resource draw needed for the request where known.

Examples:

```text
tool_calls += 1
simulation_rollouts += requested/max trial count
simulated_horizon_seconds += reserved horizon
optimizer_trials += trial budget
subagents += requested SUBTASK count
```

If the request cannot fit remaining quota, deny before adapter execution.

After execution, actual usage is reconciled against the reservation and recorded in trace/budget state. Adapters may not hide nested simulator/tool usage from declared configured dimensions.

A model state-update proposal does not consume `max_tool_calls`; runtime-v0 accounts one `max_steps` unit for each accepted/rejected update proposal batch.

## G3 — side-effect policy

Default v0 policy:

- READ / COMPUTE: eligible after G0-G2, subject to consumer policy;
- SIMULATE: eligible only when the adapter declares isolation and task policy grants sandbox execution;
- PROPOSE: may create candidate data but cannot mutate reference state;
- MUTATE: requires consumer validation and any configured authority escalation/approval;
- ADMIN: denied by default.

`SIMULATE` never mutates the reference branch. A recovery action applied to the reference branch is always `MUTATE`.

Internal `TaskStateStore` state updates are not classified as ToolSpec side effects; they are constrained by their own allowlisted consumer state operations and revision/visibility validation.

## Consumer pre-execution validator

Core runtime invokes at most one logical consumer request-validation interface for executable work; the consumer may compose internal domain validators.

```text
validate_request(
    request,
    task_policy,
    application_context,
    expected_state_revision
) -> GateDecision
```

For TEP, the lab may internally compose:

```text
lab experiment/recovery policy
+ tep-sim capability/bounds/control-mode validation
```

The generic runtime does not know those domain concepts.

The validator returns deterministic decision/reason/normalized constraints and may freeze a normalized request. It does not depend on model hidden reasoning.

Internal model-proposed state updates are validated by `TaskStateStore.apply_batch` semantics, not this executable-work hook.

## Post-execution result verification is separate

After Executor returns, the runtime/consumer uses the post-execution `verify_result` contract from `hybrid-orchestration-v0.md`.

Examples include:

- artifact/ref existence;
- result provenance;
- actual budget draw;
- state-update/result invariants.

Do not implement these as pre-execution GateDecision checks when the result does not yet exist.

## Authority escalation / approval

A higher-authority application may configure an approval/escalation step after deterministic validation.

The purpose is to authorize an already-frozen request, not ask a human to rediscover domain truth from scratch.

A pending request must preserve the exact validated parameters and expected reference-state revision.

## MUTATE revision binding

Whenever a `MUTATE` operation is enabled, validation MUST bind the frozen request to the expected reference/external-state revision/version.

Execution MUST reject the operation if that state changed between validation and application.

This is mandatory for enabled MUTATE paths, not optional TOCTOU hardening.

## Replanning after denial

A denial may be returned as structured feedback to the Main Agent if budget remains.

There is no automatic retry loop. A retry/replan is a new explicit request and consumes normal budget.

## Invariants

- Model text cannot override a gate.
- Unknown authority/policy means deny for side-effecting operations.
- Same request/policy/state/budget produces the same deterministic gate outcome.
- Denied executable operations produce no side effect.
- Child authority never exceeds explicitly delegated task/parent authority.
- SIMULATE cannot mutate reference state.
- Compound tools reserve declared nested resource consumption before dispatch.
- Domain rules remain outside generic runtime core.
- MUTATE validation is state-revision bound.
- Internal model state updates never bypass `TaskStateStore` validation and never count as executable tool authority.

## Acceptance tests

1. Invalid executable request schema fails at G0 with no adapter call.
2. Unlisted operation fails at G1.
3. Exhausted standard budget fails at G2.
4. Compound SIMULATE tool whose requested trials exceed `extra_dimensions` fails at G2 before rollout.
5. Allowed READ succeeds through request validation.
6. SIMULATE adapter without isolation guarantee is denied.
7. PROPOSE may return candidate data but cannot mutate reference state.
8. MUTATE is held for deterministic consumer validation/approval and is bound to expected state revision.
9. A stale approved MUTATE request is rejected before application.
10. Consumer denial cannot be overridden by another model message.
11. Child requesting parent-only authority is denied.
12. A ModelStateUpdateProposal is routed to TaskStateStore validation, consumes no tool-call budget, and cannot alter runtime budget/policy/authority fields.
