"""Consumer-owned state boundary; no runtime import of application state."""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from .contracts import ContextProjection, Revision, StateDelta, TaskStatus


@runtime_checkable
class TaskStateStore(Protocol):
    def revision(self) -> Revision: ...

    def status(self) -> TaskStatus: ...

    def project(self, policy: Mapping[str, Any]) -> ContextProjection: ...

    def transition_status(self, status: TaskStatus,
                          expected_revision: Revision) -> Revision:
        """Atomically persist a Coordinator-owned terminal status at this revision.

        Reject stale revisions and replacing a different terminal status without
        mutation. Same-terminal requests are no-ops. A changed status advances
        revision; the returned revision must equal revision() after application.
        This authority is never exposed as a model operation or tool.
        """
        ...

    def apply_batch(self, deltas: Sequence[StateDelta],
                    expected_revision: Revision) -> Revision:
        """Validate the complete batch, then apply all or none atomically.

        Consumer rejects stale revisions, illegal operations/fields, invalid refs,
        and visibility violations by raising an exception without changing state.
        """
        ...
