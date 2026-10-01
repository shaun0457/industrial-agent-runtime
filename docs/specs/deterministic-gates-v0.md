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

The reservation amount is the numeric declared draw, a sound request-bound upper
bound from a trusted `ReservationResolver`, or `max_budget_draw`; see "B2.1 — Request-bound
budget reservation" for the hook, order, and validation.

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

## B2 implementation notes (v0)

Implemented in `industrial_agent_runtime.gates` (pipeline), `schema` (G0 subset), and
the `Coordinator` dispatch path. No frozen public contract was changed; the B1
`RequestGate.validate_request` signature is kept as the consumer hook (D-012).

- **Task policy.** See "Accepted Batch-2 decisions" D-036 below. The default
  `GatePolicy` grants READ/COMPUTE only. ADMIN cannot be granted in v0 and MUTATE
  always requires approval. A child task without a delegated policy is denied at G1.
- **G0.** A dependency-free JSON Schema subset. Unsupported keywords deny;
  they are never ignored. Every `InformationRef`-shaped argument (any mapping
  with `ref_id`) must parse, be AGENT-visible, and exactly match a ref known to
  the run: an Agent-visible task context ref or a ref from a verified result.
- **G2.** Standard `tool_calls`/`steps` plus configured `Budget.extra_dimensions`.
  A numeric `declared_budget_draw` is reserved as-is (≤ `max_budget_draw`). The
  runtime does not interpret string (expression) draws in v0: they reserve
  `max_budget_draw` and are unreservable without it. Tools cannot declare
  runtime-charged standard dimensions. WorkBatch preflight checks the cumulative
  reservation of all items before any dispatch.
- **Reconciliation.** After execution, actual draw is charged, not the
  reservation. Over-draw, unreported reserved dimensions, unreserved dimensions,
  or invalid values are violations. A violating result is charged at least its
  reservation and is never ingested. An adapter exception is charged the full
  reservation.
- **G3 / compound SIMULATE.** Which extra dimensions represent simulation work is
  consumer configuration (`GatePolicy.simulation_dimensions`). A tool drawing them
  must be SIMULATE. A SIMULATE tool must declare an isolation guarantee and
  reserve a positive simulation draw. SIMULATE/PROPOSE/MUTATE require a consumer
  `ReferenceStateGuard`. If a non-MUTATE execution changes the reference
  revision, the run fails closed.
- **MUTATE binding / approval (OQ-5 minimal surface).** The frozen request binds
  the task-state revision, the reference revision, the ToolSpec checksum, and the
  reservation. `ApprovalHook.decide(frozen)` returns APPROVE or DENY; if no hook
  is configured, the request is denied. Immediately before dispatch, a changed
  spec, task-state revision, or (MUTATE) reference revision is rejected.
- Consumer `normalized_request_ref` is unsupported in v0 (the exact request
  is dispatched), and a consumer reservation must equal the runtime reservation.

## Accepted Batch-2 decisions

Adjudicated at the Batch-2 review closure; recorded in the program Decision Register
(`tep-sim/docs/ecosystem/decision-register.md`, D-036–D-038). They replace the three
"contract gaps resolved conservatively" flagged in `docs/b2-handoff.md`.

### D-036 — `GatePolicy` is separate from `Task`

- `Task` describes the requested work and tool surface (`allowed_tools`, budget, output).
- `GatePolicy` is trusted, application-supplied execution authority (side-effect
  class grants, policy tags, tool allowlist, approval requirements, simulation
  classification). It is **never model-authored**.
- Effective authority is the **intersection** of Task permissions and GatePolicy
  grants: a tool must be in `Task.allowed_tools` *and* allowed by every policy in the
  delegation chain, and its class/tags must be granted.
- Delegated child policies (`GatePolicy.delegate`):
  - tools, side-effect classes, and policy tags may only **narrow**;
  - approval requirements may only become **stricter** (superset);
  - `simulation_dimensions` are classification semantics, not child authority, and
    MUST be **inherited unchanged** in v0. A child that redefines them (adds, drops,
    or clears dimensions) is rejected at construction.

### D-037 — Dynamic budget draws are not evaluated in v0

- There is no expression language in v0.
- Numeric `declared_budget_draw` → reserve that numeric value (≤ `max_budget_draw`
  when one is declared).
- String/dynamic declaration → the runtime does not evaluate it; the dimension
  requires `max_budget_draw` and **reserves the maximum**.
- Missing maximum → denied at G2 as `UNRESERVABLE_DRAW`.
- Actual usage is reconciled after execution (see Reconciliation above).

B2.1 (D-048, below) refines only the reservation *amount* for a string declaration
when a trusted resolver supplies a sound request-bound upper bound. D-037 is
unchanged: the runtime still never parses or evaluates the string, and the maximum
remains the fallback.

## B2.1 — Request-bound budget reservation (D-048)

Resolves C4 SC-5. Reserving `max_budget_draw` for every dynamic dimension is safe
but over-reserves: a request whose conforming execution is bounded far below the
maximum can be denied although it fits the remaining quota. B2.1 lets a **trusted,
application-supplied** component state a request-bound reservation for one specific
validated request.

### Meaning of a request-bound reservation

A request-bound reservation is a **deterministic amount derived from the specific
request that soundly upper-bounds the resource use of any conforming execution of
that request**. It is not a prediction of actual consumption. It is exact only in
the sense that it is the precise amount G2 admits and reserves.

```text
requested horizon   = 600 seconds
reservation         = 600 seconds   (REQUEST_BOUND; admitted by G2)
actual successful   = 450 seconds   (charged at reconciliation; 150 released)
```

- Actual usage may be lower than the reservation.
- Normal conforming execution must not exceed it. Exceeding it is a reconciliation
  violation (see Invariants).
- The resolver derives the bound only from its trusted allowed inputs (the validated
  request and the ToolSpec).
- The bound is finite, non-negative, and `<= max_budget_draw`.
- If the resolver cannot derive a sound upper bound for a dynamic dimension, it
  **MUST omit that dimension**, which keeps the D-037 `max_budget_draw` fallback.
- Probabilistic, typical-case, or otherwise optimistic estimates are not valid
  request-bound reservations and must never be used for G2 admission.

### Hook and order

This is the actual evaluation order. Denial precedence follows it: a request that
fails both the G2 quota and a G3 rule is denied at `G2_BUDGET`, and G3 is not
evaluated. One pre-existing exception is unchanged: a malformed ToolSpec draw
declaration (e.g. declared draw above its maximum, or a standard dimension) is
detected during G2a sizing but reported as `G0_SCHEMA`/`INVALID_TOOL_SPEC`.

```text
ToolCallRequest
 -> G0 schema / refs
 -> G1 allowlist / authority
 -> G2a static sizing (D-037: numeric declared draw, else max_budget_draw)
 -> G2b trusted request-bound resolution (ReservationResolver, dynamic dimensions only)
 -> G2c validate the resolved bound; check the reservation against remaining quota
 -> G3 side-effect policy (judged on the reservation that passed G2)
 -> consumer.validate_request
 -> approval if required
 -> FrozenRequest (carries the admitted reservation and its per-dimension origin)
 -> dispatch-time rebinding
 -> Executor
```

In code, `GatePipeline.static_check` runs G0, G1, G2a, and G2b plus resolved-bound
validation. `GatePipeline.check_budget` runs the G2 quota check, and only then
`GatePipeline.check_side_effect` runs G3. `authorize` runs that sequence for one
request. WorkBatch preflight sizes every item (G0–G2b), runs its structural checks
(duplicate ids, dependencies, cycles, budget requests) and the cumulative G2 quota
check, then G3 for every item in lexical `work_id` order, all before any dispatch.
The Coordinator rejects a reused `request_id` or missing execution hooks before G2
quota/G3, as plain runtime denials.

```text
ReservationResolver.resolve_reservation(
    request: ToolCallRequest,   # already passed G0 and G1
    spec: ToolSpec
) -> map<dimension, number>     # sound request-bound upper bounds
```

- The resolver is configured by the application (`Coordinator(reservation_resolver=...)`,
  `GatePipeline(..., resolver=...)`). It is **never model-authored** and is distinct
  from `RequestGate.validate_request`; `GateDecision` is not extended.
- It is invoked only when the ToolSpec has at least one dynamic (string) declared
  draw. Specs with only numeric or maximum-only draws never call it.
- It receives only the validated request and the ToolSpec: no transcript, model
  reasoning, task state, or budget usage. It must be a pure function of those
  inputs. It must not execute tools, grant authority, mutate task/reference state,
  or evaluate expressions.
- It may return a subset of the dynamic dimensions. An omitted dimension falls back
  to `max_budget_draw` (D-037).

### Validation (all at `G2_BUDGET`, before G3 and before any adapter call)

| Condition | Reason code |
|---|---|
| resolver raises | `RESOLVER_ERROR` |
| output is not a mapping with string keys | `INVALID_RESOLVER_OUTPUT` |
| key not declared by the ToolSpec | `RESOLVED_DIMENSION_UNDECLARED` |
| key is declared but not dynamic (numeric or maximum-only) | `RESOLVED_DIMENSION_NOT_DYNAMIC` |
| value is not a finite non-negative number (incl. bool, NaN, ±Inf, negative) | `INVALID_RESOLVED_DRAW` |
| value exceeds `max_budget_draw` | `RESOLVED_DRAW_EXCEEDS_MAX` |
| dynamic dimension without `max_budget_draw` | `UNRESERVABLE_DRAW` (unchanged; checked first) |
| reservation recomputed at authorization differs from the preflight reservation | `RESERVATION_NOT_DETERMINISTIC` |
| reservation does not fit remaining quota | `BUDGET_EXHAUSTED` (unchanged) |

### Reservation origin

Every reserved dimension carries one origin:

```text
DECLARED       numeric declared_budget_draw (static)
REQUEST_BOUND  request-bound upper bound from the trusted resolver for this request
MAX_FALLBACK   dynamic declaration (or None / maximum-only) reserved at max_budget_draw
```

`FrozenRequest.reservation_origins` and the `EXECUTE` trace event record the origin
map next to the reserved amounts. No resolver internals are recorded.

### Invariants

- A smaller request-bound reservation never authorizes a request. G1, G3 (class,
  tags, simulation classification/isolation), consumer validation, approval, and
  MUTATE revision binding are evaluated exactly as before. G3's positive
  simulation-draw requirement applies to the resolved reservation.
- The reservation is computed before freeze and dispatched from `FrozenRequest`.
  Approval sees the same amounts and origins, and the reservation is never
  recomputed after freeze.
- The Coordinator sizes each request in preflight and requires authorization to
  reproduce exactly that reservation. The resolver is therefore called twice per
  request (preflight sizing, then authorization) and must be pure. Drift is
  reported at G2.
- A resolver whose answer drifts after WorkBatch preflight fails closed for that
  item only (`RESERVATION_NOT_DETERMINISTIC`). Under `ALL_SETTLED`, already
  dispatched items stand and dependents are `SKIPPED_DEPENDENCY`. No item is ever
  dispatched with a reservation other than the one counted in the cumulative
  preflight.
- WorkBatch preflight sums the resolved per-item reservations (plus standard
  counters). It denies the whole batch before any dispatch, and before any G3
  evaluation, when the sum does not fit.
- Reconciliation (B2/B3) is unchanged:
  - actual usage is charged, so a lower actual use releases the remainder;
  - an overdraw beyond the reservation and unreserved/unreported dimensions are
    violations;
  - an adapter exception is charged the full (resolved) reservation;
  - violating results are not ingested.
- Accepted v0 residual: `Executor.execute(request, spec)` does not receive the
  reservation. Trusted adapters are expected to obey the bounded request semantics.
  A successful overdraw beyond the request-bound reservation remains a
  reconciliation violation, is charged `max(actual, reserved)`, and is never
  ingested. Unknown actual use is charged the resolved reservation, as in B2.
- A consumer `GateDecision.reserved_budget_draw`, when present, must equal the
  resolved reservation (B2 rule unchanged).
- Generic runtime interfaces name only ToolSpec resource dimensions. Any domain
  interpretation of request arguments belongs to the consumer's resolver.

### D-038 — Two revision domains

- `GateDecision.expected_state_revision` = the consumer `TaskStateStore`/task-state
  revision (unchanged).
- `FrozenRequest.expected_state_revision` = task/investigation state revision.
- `FrozenRequest.expected_reference_revision` = external/reference-world revision
  (from the consumer `ReferenceStateGuard`).
- One field is never overloaded to represent both domains.
- An enabled MUTATE request binds **both** revisions in the frozen request presented
  for approval, and both are re-checked immediately before dispatch after approval:
  a changed task-state revision denies `STALE_STATE_REVISION`, a changed reference
  revision denies `STALE_REFERENCE_REVISION`, and a MUTATE without a bound
  reference revision denies `REFERENCE_REVISION_UNBOUND`.
