# ADR-001 — Start with a minimal explicit executor

Status: **superseded by ADR-002 / `hybrid-orchestration-v0.md`**  
Date: 2026-09-15

## Historical context

The initial runtime design assumed that bounded model/tool/subagent orchestration could be represented by a small explicit Python state machine, with LangGraph added only if durable resume/interrupt requirements later appeared.

This was useful for establishing several principles that remain valid:

- keep framework-native types out of public contracts;
- keep deterministic gates explicit/testable;
- do not use CLI coding agents or recursive swarms as the production runtime;
- preserve a small simple execution path for simple tasks.

## Original decision

The original proposal was to implement v0 primarily as an explicit Python executor/state machine and treat LangGraph as a later optional adapter.

## Why superseded

Subsequent architecture discussion clarified that the target system intentionally needs a **Hybrid orchestration model**:

- goal-driven Main Agent;
- deterministic Coordinator / Executor / Verifier;
- local ReAct-style simple path;
- bounded model-proposed Dynamic DAG for complex investigations;
- structured Investigation State / Information Plane;
- graph/checkpoint orchestration useful in `tep-agent-lab`.

Therefore "minimal explicit executor" is no longer the complete runtime architecture.

## What remains from this ADR

A small explicit loop/state machine remains valuable as:

- the simplest reference implementation of the framework-neutral contracts;
- a deterministic unit-test harness;
- the fast/simple path that does not need a Dynamic DAG;
- a comparison baseline against a LangGraph adapter.

It must not be interpreted as a prohibition on Hybrid graph orchestration.

## Superseding decision

See:

- `ADR-002-hybrid-orchestration.md`;
- `../specs/hybrid-orchestration-v0.md`;
- `../architecture.md`.
