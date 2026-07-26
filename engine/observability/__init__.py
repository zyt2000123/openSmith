"""Local-first execution observability primitives.

This package owns the durable, append-only record of an Agent run, its
aggregate projections, and all supported read access. Execution code uses
an observer protocol; composition code injects ``RunObservation`` and service
code uses ``ObservabilityReader``. Neither needs to know the trace or summary
storage layout.
"""

from .diagnosis import RunDiagnosis, RunDiagnoser
from .health import AgentHealth, HealthCalculator
from .incidents import IncidentDetector, RunIncident
from .projections import RunSummary, RunSummaryProjection
from .proposals import ImprovementProposer, RunImprovementProposal
from .recorder import RunEventRecorder
from .reader import ObservabilityReader
from .runtime import RunObservation
from .summary_store import RunMetadata, RunSummaryRecord, RunSummaryStore
from .trace_store import TraceStore

__all__ = (
    "AgentHealth",
    "HealthCalculator",
    "IncidentDetector",
    "ImprovementProposer",
    "RunEventRecorder",
    "RunDiagnosis",
    "RunDiagnoser",
    "RunIncident",
    "RunImprovementProposal",
    "RunObservation",
    "RunMetadata",
    "RunSummary",
    "RunSummaryRecord",
    "RunSummaryProjection",
    "RunSummaryStore",
    "TraceStore",
    "ObservabilityReader",
)
