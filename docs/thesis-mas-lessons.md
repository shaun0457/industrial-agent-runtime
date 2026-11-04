# Lessons from the predecessor multi-agent system

Date: 2026-11-04. Status: reference note, not a spec. It changes no contract in `docs/specs/`.

## Source

This runtime replaces the fixed Supervisor + specialist team in `master_thesis`, a LangGraph MAS for TEP fault diagnosis. The full audit is `master_thesis/docs/upgrade/05-agentic-gap-audit.md`:

- four models ran the same investigation independently;
- each finding was checked against the code.

This note keeps only the domain-neutral failure classes and records how this runtime handles each one. Its purpose is to keep these failure classes closed when the first real provider (OQ-1, roadmap Phase 5) lands.

Why the predecessor was dropped (owner's account, 2026-11-04): context was lost at every Supervisor ↔ specialist handoff. Two options were considered:

1. keep the multi-agent team, with a coordinating main agent, executor agents, shared state and a dynamic DAG;
2. use a single main agent.

ADR-002 took option 2 for v0 and kept an entry point for option 1:

- `WorkBatch` with `depends_on`;
- bounded `SUBTASK` items;
- the rich Dynamic DAG, deferred to OQ-9.

## Failure classes and where they stand here

| # | Failure in the predecessor (verified in its code) | Runtime handling | Status |
|---|---|---|---|
| L1 | Context lost across agent handoffs | Single Main Agent; reasoning state lives in `TaskStateStore`, not in the conversation; subtasks return a compact `SubtaskResult`, not a transcript (ADR-002, `subagents-v0.md`) | Closed by design |
| L2 | Grounding audit ran **after** the answer; a BLOCKING verdict was printed but did not stop the answer | Pre-execution gates G0–G3; post-execution `verify_result` rejects a result before ingestion; `FinishProposal` goes through deterministic readiness checks (OQ-8) | Closed for tool results. The finish-readiness checks are consumer-owned: the consumer must check that every entity the answer names exists in the domain schema |
| L3 | A judge or audit **exception** was treated as "no issues", and the conclusion was stored as grounded | A verifier exception rejects that result (`coordinator.py:257`, `VERIFICATION_ERROR`); verification is fail-closed (`verification.py`) | Closed. Keep a test for this when adding verifiers |
| L4 | Uncited sentences were back-filled with citations taken round-robin from a source pool, which inflated citation coverage | Runtime never promotes output to evidence. In the consumer (tep-agent-lab), an observation becomes evidence only when it is explicitly linked to a hypothesis | Closed by design. Do not add any automatic evidence or citation attachment |
| L5 | The judge was the same model instance as the agent | The Verifier is deterministic; an LLM critic is optional, advisory only, and cannot override it (OQ-6) | Closed. If OQ-6 adds a critic, use a different model family |
| L6 | One provider; no timeout, retry or fallback on the agent or judge path | Not implemented yet (OQ-1, roadmap Phases 5 and 7). Work items have no silent retry, by design | **Open** |
| L7 | Only the CLI path had a checkpointer; the API path could not resume | Reference loop only; persistent checkpoint backend is OQ-4 | **Open** |
| L8 | No human approval interrupt | Authority-escalation interface is OQ-5, needed only for MUTATE | **Open** (not needed for read-only blind RCA) |
| L9 | Stall detection was literal dedup only; 15 of 180 thesis runs hit the 50-turn cap (Debate protocol 31.1 %) | Hard budgets in the Coordinator; semantic stop policy is OQ-8 | **Open**. Add a stall or oscillation metric |
| L10 | Strictly sequential delegation | Dependency-aware `WorkBatch` with parallel ready items | Closed (TOOL items). SUBTASK execution is still fail-closed until the consumer integration lands |
| L11 | Correctness was never measured: a keyword gate and a dry run only; eval questions contained the answer (fault ID) | Owned by the consumer: tep-agent-lab `evaluation-v0.md`, `d0-benchmark-contract.md` (blind cases, LeakageAudit, mandatory C0 baseline) | Covered by the consumer specs; real-LLM runs not done yet |

## Checks to run when the first real provider lands

The runtime-side items (provider adapter, budgets, traces) belong in the adapter PR. The rest are consumer-side and belong in tep-agent-lab.

1. **Provider transport** (L6):
   - a client timeout;
   - bounded retry with backoff for transport and rate-limit errors only;
   - an optional fallback model.

   Count retries in the budget and trace them. This is distinct from work-item retry, which stays an explicit Main Agent request.
2. **Fail-closed paths** (L3): a test for each, covering
   - provider error;
   - malformed structured output;
   - verifier exception.

   Each must end as a rejected turn or result, never as a successful finish.
3. **Finish readiness** (L2, consumer): a blind case with a non-existent entity in the prompt, the analogue of the thesis XMEAS_46 trap. Report both the block rate and the false-block rate.
4. **Repeated trials** (L11, consumer):
   - run each blind case k times and report pass^k next to the C0 baseline;
   - run a single-agent-without-WorkBatch arm, as ADR-002 already plans.
5. **Stall signal** (L9): record repeated identical requests and state-revision churn per run, so that OQ-8 has data.

## External references (2026-11)

- MAST failure taxonomy, arXiv 2503.13657.
- Scaling Agent Systems, arXiv 2512.08296: multi-agent setups help parallelisable work and hurt sequential planning; independent agents amplify errors.
- Anthropic, "Building agents: When to fold subagents into your main agent loop" (2026-10-22): a single agent with skills often beats subagent designs; keep subagents for context isolation or parallel work.
- tau-bench pass^k as the reliability metric.

These agree with ADR-002, which predates the 2026-10-22 article. They are corroboration, not the reason for the decision.
