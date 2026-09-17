# B1 handoff — independent contract tranche

Branch: `feat/contracts-runtime-v0`.

Status: **partial delivery; B1 exit gate blocked by SPEC_CONFLICT**.

## Changes

- `pyproject.toml`, `.gitignore`: Python 3.11+ standard-library-only package scaffold.
- `src/industrial_agent_runtime/contracts.py`: frozen generic InformationRef,
  Budget, Task, ToolSpec/ToolResult, TaskStatus, StateDelta,
  ModelStateUpdateProposal, ContextProjection, RuntimeResult, Subtask/SubtaskResult,
  and TraceEvent contracts. Mutable JSON inputs are defensively copied/frozen.
- `protocols.py`: consumer-owned `TaskStateStore` protocol with atomic
  `apply_batch`; runtime imports no consumer state.
- `serialization.py`: deterministic JSON conversion/checksum and rejection of
  opaque objects/nonfinite values.
- `trace.py`: durable single-writer append-only JSONL events; exact immutable
  content-addressed projection artifacts; projection corruption/path validation;
  required MODEL_TURN metadata and task/ref linkage checks.
- `tests/test_contracts.py`: public contract, visibility, consumer protocol,
  serialization, durable replay, immutability, and boundary tests.
- `docs/b1-spec-conflicts.md`: evidence and minimal proposed contract decisions.

All public implemented names are re-exported from `industrial_agent_runtime`.
`Revision` is `int | str`; producer labels and domain payloads remain opaque.
`to_jsonable` converts frozen mappings/tuples/dataclasses to plain JSON values.

## Verification

```powershell
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
py -3.13 -m unittest discover -s tests -v
```

Result: `Ran 15 tests in 0.079s` / `OK` (exit 0).
This is the complete current regression suite; the baseline contained no code or
tests. Tests require no network, external provider, domain package or framework.

## Acceptance coverage and limitations

- Implemented contracts are serializable, immutable at the JSON payload boundary,
  and independent of all application/domain imports.
- Consumer-owned fake store demonstrates atomic all-or-nothing multi-delta
  application and optimistic stale-revision rejection. This validates protocol
  usability, **not** an implemented runtime loop or a production consumer store.
- Trace recorder persists/reopens exact projection bytes and required model/tool
  metadata. Fixed inputs produce identical artifact/event bytes.
- EVALUATOR refs are rejected in projection included_refs. Consumer content
  relevance/recursive domain visibility and semantic reference resolution remain
  consumer responsibilities; no generic semantic leakage detector is claimed.
- No gates, Executor, real provider, subagent spawning, or mutable DAG was added.
- Trace recorder is a single-writer per-run primitive, not crash recovery or
  cross-process concurrency infrastructure. Checksums detect changed artifact
  bytes; they are not an OS access-control guarantee.
- ModelTurn action variants, fake provider, reference loop, same-turn suppression,
  step accounting, and WorkBatch scheduling/ingestion acceptance are unimplemented
  pending the three public-contract decisions in `b1-spec-conflicts.md`.
- B2/B3/B4 remain downstream. No executable bypass/fallback was introduced.

## Integration

Pin this branch commit for independent lab imports of InformationRef,
ContextProjection, StateDelta, TaskStateStore and JSON helpers. Do not advertise
this package as a completed B1 runtime or start dependent execution tests until
the owning contracts are adjudicated and the blocked slice is completed.
