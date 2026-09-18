"""Deterministic offline provider for every typed action and update variant."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .actions import ModelTurn
from .contracts import ContextProjection, ToolSpec


class FakeProvider:
    """Replay typed turns or deterministic factories without provider/network I/O.

    Factories receive the exact immutable projection and call limits. The latter
    includes its durable context_projection_ref, so a factory can bind a fresh
    typed turn without predicting an artifact checksum. Exhaustion is explicit.
    """

    def __init__(self, turns: Iterable[
            ModelTurn | Callable[[ContextProjection, Mapping[str, Any]], ModelTurn]]):
        self._turns = iter(turns)
        self.projections: list[ContextProjection] = []

    def generate(self, context_projection: ContextProjection,
                 tool_specs: Sequence[ToolSpec], output_schema: Mapping[str, Any],
                 limits: Mapping[str, Any]) -> ModelTurn:
        self.projections.append(context_projection)
        try:
            turn = next(self._turns)
        except StopIteration as exc:
            raise RuntimeError("fake provider script exhausted") from exc
        return turn(context_projection, limits) if callable(turn) else turn
