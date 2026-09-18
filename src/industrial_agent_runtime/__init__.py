"""Industrial Agent Runtime's framework-neutral public state contracts."""

from .contracts import (
    Budget, ContextProjection, InformationRef, ModelStateUpdateProposal, Revision,
    RuntimeResult, SideEffectClass, StateDelta, Subtask, SubtaskResult, Task,
    TaskStatus, ToolResult, ToolSpec, TraceEvent, Visibility,
)
from .protocols import TaskStateStore
from .serialization import canonical_json, checksum, to_jsonable
from .trace import TraceRecorder
from .actions import Action, FinishProposal, ModelTurn, ToolCallRequest, WorkBatch, WorkItem
from .coordinator import Coordinator
from .hooks import (Executor, GateDecision, ModelProvider, RequestGate,
                    ResultIngestor, ResultVerifier)
from .provider import FakeProvider

__all__ = [
    "Budget", "ContextProjection", "InformationRef", "ModelStateUpdateProposal",
    "Revision", "RuntimeResult", "SideEffectClass", "StateDelta", "Subtask",
    "SubtaskResult", "Task",
    "TaskStateStore", "TaskStatus", "ToolResult", "ToolSpec", "Visibility",
    "TraceEvent", "TraceRecorder", "canonical_json", "checksum", "to_jsonable",
    "Action", "Coordinator", "Executor", "FakeProvider", "FinishProposal", "GateDecision",
    "ModelProvider", "ModelTurn", "RequestGate", "ResultIngestor", "ResultVerifier",
    "ToolCallRequest", "WorkBatch", "WorkItem",
]
