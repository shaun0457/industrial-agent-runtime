"""Consumer-owned state boundary; no runtime import of application state."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .contracts import ContextProjection, Revision, StateDelta, TaskStatus


@runtime_checkable
class TaskStateStore(Protocol):
    def revision(self) -> Revision: ...

    def status(self) -> TaskStatus: ...

    def project(self, policy: Mapping[str, Any]) -> ContextProjection: ...

    def apply_batch(self, deltas: Sequence[StateDelta],
                    expected_revision: Revision) -> Revision:
        """Validate the complete batch, then apply all or none atomically.

        Consumer rejects stale revisions, illegal operations/fields, invalid refs,
        and visibility violations by raising an exception without changing state.
        """
        ...
