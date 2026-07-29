from typing import Literal

from pydantic import BaseModel

MaintenanceState = Literal["idle", "pending", "running"]


class MemoryMaintenanceOut(BaseModel):
    """Whether deferred memory maintenance is running or still owed.

    Compilation, periodic candidate curation, and dreaming are deferred to
    background tasks that outlive the turn that scheduled them, so they cannot
    be reported over a per-run event stream. This is the state a client polls.
    """

    compile: MaintenanceState
    nudge: MaintenanceState
    dream: MaintenanceState
