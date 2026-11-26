"""Model-input context lifecycle: assembly, budgeting, and session compaction."""

from .action_ledger import (
    ActionOutcome,
    DroppedActionLedger,
    LedgerEntry,
    build_action_outcome,
    is_elision_note,
)
from .assembler import (
    AssembledPrompt,
    PromptAssembler,
    PromptLayer,
    PromptManifest,
    PromptPlan,
)
from .budget import ContextBudget
from .compression import (
    CONTEXT_DISPLAY_WINDOW,
    compact_history,
    compress,
    estimate_tokens,
    needs_compaction,
    prompt_budget_for_llm,
)
from .fitting import (
    ContextFitResult,
    ContextFitStatus,
    ContextReceipt,
    fit_request,
    measure_request,
)
from .summary import (
    SESSION_SUMMARY_PREFIX,
    SessionSummaryResult,
    SessionSummaryStatus,
    summarize_session,
)

__all__ = (
    "ActionOutcome",
    "AssembledPrompt",
    "CONTEXT_DISPLAY_WINDOW",
    "DroppedActionLedger",
    "LedgerEntry",
    "SESSION_SUMMARY_PREFIX",
    "ContextBudget",
    "ContextFitResult",
    "ContextFitStatus",
    "ContextReceipt",
    "PromptAssembler",
    "PromptLayer",
    "PromptManifest",
    "PromptPlan",
    "SessionSummaryResult",
    "SessionSummaryStatus",
    "build_action_outcome",
    "compact_history",
    "compress",
    "estimate_tokens",
    "fit_request",
    "is_elision_note",
    "measure_request",
    "needs_compaction",
    "prompt_budget_for_llm",
    "summarize_session",
)
