# B1 public-contract gaps

Status: **RESOLVED — accepted as D-034 on 2026-09-18**.

The owning runtime specs now define the accepted minimal contracts exactly as
recorded below. Dependent B1 implementation may resume. This record is retained as
implementation evidence and must not be read as an active blocker.

At the original report, no canonical spec had been changed and only independent
state/ref/serialization/trace contracts were implemented. D-034 subsequently
resolved the gaps; resumed B1 implementation is recorded in `b1-handoff.md`.
The historical evidence below is retained for audit.

## Evidence search

The following read-only searches were performed in the runtime worktree and the
program repository, including the frozen blueprints:

```powershell
git grep -n -E 'ToolCallRequest|FinishProposal|completion_policy|tool_name|request_id' -- docs README.md
git -C ../../tep-sim grep -n -E 'ToolCallRequest|FinishProposal|completion_policy' -- docs/ecosystem docs/specs
```

The runtime's `docs/architecture.md:167` only lists `ToolCallRequest / ToolResult`.
The README and program charter only reference action names. The lab state spec
describes structural readiness and runtime ownership of finish, not a generic
request payload. No alternate owning definition was found.

## 1. ToolCallRequest has no field contract

- Affected spec: `docs/specs/runtime-v0.md:130–132`.
- Evidence: the entire definition is "Model-produced typed request. Parse/schema
  validity never grants execution authority." The adjacent `request_id` at line
  138 belongs to **ToolResult**, not ToolCallRequest.
- Conflicting assumptions: implementing G0 and dispatch requires identifying a
  tool and its arguments, while the frozen public request has no names or types
  for these fields. Choosing `tool` versus `tool_name` or a nested operation
  envelope in code would silently create the public wire contract.
- Accepted smallest change: define
  `{request_id: string, tool_name: string, arguments: JSON object}` and explicitly
  state that model-produced fields contain no execution authority.

## 2. FinishProposal payload is undefined

- Affected specs: `docs/specs/runtime-v0.md` ModelTurn;
  `docs/specs/hybrid-orchestration-v0.md` post-execution verification / reference loop;
  `docs/open-questions.md:62`.
- Evidence: the model action includes `finish_proposal?`; only diagrams and prose
  refer to `FinishProposal`, without a field definition.
- Conflicting assumptions: a typed final-output verifier needs the proposed
  output plus reference set, but no generic representation is prescribed.
- Accepted smallest change: define
  `{structured_output: JSON value, information_refs: InformationRef[],
  artifact_refs: InformationRef[]}`; readiness remains consumer-defined.

## 3. WorkBatch completion_policy has no values or behavior

- Affected spec: `docs/specs/hybrid-orchestration-v0.md:241`.
- Evidence: `completion_policy` is listed as a required field without type,
  supported values, or selection rules. Existing failure semantics define failed
  dependencies and skipped dependents but do not define the policy field.
- Conflicting assumptions: accepting any value gives unknown model output
  meaning; ignoring it could execute work contrary to requested policy.
- Accepted smallest change: v0 supports only `ALL_SETTLED`,
  using the existing failure/dependency rules; unknown values are denied.

## Resume gate

D-034 and the owning spec updates satisfy the resume gate. Implement the blocked
models and loop, then run the original B1 acceptance criteria. The earlier partial
contract tranche alone remains non-execution-authorized.
