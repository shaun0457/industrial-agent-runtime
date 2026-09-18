# B1 handoff — contracts and reference coordinator

Branch: `feat/contracts-runtime-v0-resume`.
Base: `83b8645`, containing approved D-034 contract closure.

Status: B1 implemented with restricted, fail-closed B2/B3/B4 seams.
No unresolved SPEC_CONFLICT. Downstream runtime phases remain unfinished.

## Changes

- `actions.py`: immutable D-034 ToolCallRequest, FinishProposal, ModelTurn action
  union, TOOL/SUBTASK WorkItem and WorkBatch. Only ALL_SETTLED is accepted.
- `provider.py`: deterministic FakeProvider scripts/factories for every action and
  state proposal, bound to exact projection refs.
- `hooks.py`: trusted RequestGate, Executor, ResultVerifier, ResultIngestor,
  ModelProvider protocols and canonical GateDecision envelope.
- `coordinator.py`: single-run loop, atomic revision-bound model updates, structured
  feedback, TOOL waves, failure propagation, current-revision lexical ingestion,
  lifecycle/budget counters, and complete durable event/projection tracing.
- `__init__.py`: exports; original state/ref/store contracts remain compatible.
- `tests/test_coordinator.py`: end-to-end fake-provider acceptance/negative paths.
- README, this handoff, historical conflict record: current integration status.

## Verification

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
& C:/Users/chengting/AppData/Local/Programs/Python/Python313/python.exe -m unittest discover -s tests -q
```

Output: `Ran 52 tests in 1.528s` / `OK` (exit 0), including all 15 original
contract/trace regressions. No network, provider SDK, domain package, LangGraph or
MCP is needed. AST import-boundary regression passes.

## B1 acceptance

- Typed no-tool finish and all four ModelTurn actions run through FakeProvider.
- Atomic two-delta proposal commits once before a same-turn tool gate sees the new
  revision. Illegal/stale/malformed proposals apply nothing and suppress direct
  tools and entire WorkBatch actions from that turn.
- Accepted/rejected update batches use one step and zero tool calls. Dispatched
  tools use one step and one tool call; NONE/finish use model-call budget only.
  Step/model exhaustion stops; unsupported token-metered execution fails before
  provider calls, rather than treating approximate tokens as actual usage.
- Batches validate unique IDs, dependencies, cycles, cumulative standard budgets,
  ALL_SETTLED and recursive hidden ref envelopes before scheduling. A later hidden
  EVALUATOR ref prevents even the first otherwise legal work item from dispatching.
- Failed items propagate SKIPPED_DEPENDENCY without retries; independent successful
  results still ingest. The OQ-3 permitted sequential Executor collects a complete
  ready wave before lexical work_id ingestion. Gate revisions [0, 0, 2] versus
  ingestion revisions [0, 1, 2] prove current-revision rebinding.
- Consumer fake TaskStateStore implements atomic optimistic validation; runtime
  imports no consumer class. Model producer becomes MODEL; ingestion producer
  becomes RESULT_INGESTION before consumer validation.
- Exact saved projections contain task_state plus runtime_feedback. Every model
  call records provider/model/version/prompt/tool metadata. Complete turns,
  proposals, raw tool results, verified deltas and resulting revisions persist in
  JSONL. A fixed clock, tools and script produce identical trace bytes.
- Missing hooks, unknown tools, schema/consumer denial, unknown result/final refs,
  hidden refs, verifier exceptions and unexpected actual resource use fail closed.

## Integration and downstream limits

- Coordinator requires an unused trace directory and runs once. Trusted hooks are
  application code, never model-authored callbacks. No allow-all default exists.
- `generate(..., limits)` receives exact context_projection_ref, immutable Budget
  and copied usage. Fake factories use that ref to bind fresh typed turns.
- B1 permits READ/COMPUTE with zero declared/reserved extra draw only. Gate output
  must explicitly ALLOW the exact request at the current revision. Approval,
  unbound revision and unsupported normalization/reservation deny execution.
- Full JSON-schema and consumer-condition checks remain the mandatory per-request
  gate responsibility immediately before dispatch (B2). Generic batch preflight
  only inspects structural/ref-envelope visibility; semantic or string-reference
  resolution belongs to the consumer. No new batch gate interface was invented.
- Post-result schema/ref/provenance/ingestion invariants and finish structural
  readiness remain mandatory trusted verifier responsibilities (B3). Tests supply
  deterministic fixture validators, not production domain policies.
- SIMULATE/PROPOSE/MUTATE/ADMIN, compound resource reservation/accounting,
  token-metered providers and SUBTASK spawning remain disabled pending B2-B5.
  Configured extra dimensions are preserved and never silently spent.
- Successful ToolResult status in this adapter is SUCCESS. Other statuses do not
  ingest. RuntimeResult owns coordinator lifecycle; consumer stores own their
  application status and legal delta operations.
- Trace is single-writer audit persistence, not checkpoint/resume. Executor hooks
  own adapter timeout/resource enforcement. No shell, background worker, mutable
  graph, domain code or provider network access was added.

Coordinator independent review requested generic whole-batch hidden-ref preflight
and a finish-verifier exception regression. Both are implemented and included in
this verification. Commit SHA is supplied separately in the task handoff.
