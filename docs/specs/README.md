# Industrial Agent Runtime Specifications

These v0 specs define the future `industrial-agent-runtime` repository.

Phase 0 Design Freeze is complete for the runtime/lab v0 boundary. See `../../../../design-freeze-record.md` and `../../../../implementation-plan.md` for release status.

- [`runtime-v0.md`](runtime-v0.md) — core Task/Budget/Tool/ModelTurn/StateDelta/ContextProjection/TaskStateStore contracts, including explicit ModelStateUpdateProposal semantics.
- [`hybrid-orchestration-v0.md`](hybrid-orchestration-v0.md) — Main Agent + deterministic Coordinator/Executor/post-verifier, local ReAct behavior, explicit state-update routing, deterministic result ingestion, and dependency-aware WorkBatch semantics.
- [`deterministic-gates-v0.md`](deterministic-gates-v0.md) — generic executable-request schema/permission/budget/side-effect gate model; internal task-state updates use the separate TaskStateStore path.
- [`subagents-v0.md`](subagents-v0.md) — bounded ephemeral-subagent semantics and `SubtaskResult` contract.

The runtime is domain-independent. TEP topology, Rule Registry content, process safety truth, intervention limits, RCA/HAZOP/recovery logic, benchmark fixtures, Engineering Records, and AutoResearch scoring remain in consumer/domain repositories.

The public contracts are framework-neutral. LangGraph/MCP are not v0 dependencies.
