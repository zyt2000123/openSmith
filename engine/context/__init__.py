"""Model-input context lifecycle: assembly, budgeting, and session compaction."""

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
    "AssembledPrompt",
    "CONTEXT_DISPLAY_WINDOW",
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
    "compact_history",
    "compress",
    "estimate_tokens",
    "fit_request",
    "measure_request",
    "needs_compaction",
    "prompt_budget_for_llm",
    "summarize_session",
)
