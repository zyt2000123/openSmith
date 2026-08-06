from typing import Literal

from pydantic import BaseModel

MaintenanceState = Literal["idle", "pending", "running"]


class MemoryMaintenanceOut(BaseModel):
    """Whether deferred memory maintenance is running or still owed.

    Compilation, periodic candidate curation, and dreaming are deferred to
    background tasks that outlive the turn that scheduled them, so they cannot
    be reported over a per-run event stream. This is the state a client polls.

    ``consecutive_failures`` counts the trailing run of failed automatic memory
    operations and ``last_error`` carries the newest sanitized failure, so a
    stalled pipeline (a provider key answering 401 on every compile) is visible
    instead of starving memory silently.
    """

    compile: MaintenanceState
    nudge: MaintenanceState
    dream: MaintenanceState
    # The derived topic-knowledge lane runs inside compile, so it is only ever
    # idle or pending; pending means a failed sync owes a retry.
    topic_sync: MaintenanceState = "idle"
    consecutive_failures: int = 0
    last_error: str | None = None
