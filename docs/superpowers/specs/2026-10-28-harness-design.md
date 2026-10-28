# Agent Harness SDD

Status: accepted for implementation. Scope: the shared Python ReAct execution path.

## Problem and outcome

Tool output archives currently overwrite one another when the same tool runs twice
within a second. Successful polling with changing results is treated as repetition.
Tool execution also inserts runtime notes between results of one assistant batch,
which strict providers reject. The model loop owns all of these responsibilities.

Split output capture, batch execution, and progress observation into independent
modules while preserving the public `react_event_loop` event stream. The design
uses responsibilities observed during the Octok analysis; implementation is native
to openSmith, with no extracted application code or binaries included.

## Contracts and acceptance criteria

| ID | Requirement | Acceptance evidence |
|---|---|---|
| O1 | Each oversized output has a distinct, immutable archive reference; concurrent calls cannot overwrite one another. | Output collision and parallel capture tests |
| O2 | Archives are redacted and created private (0600); names cannot escape the output directory. Save failures return a truthful, bounded preview. | Secret, permission, path, write failure tests |
| O3 | Preview respects UTF-8 byte and line limits even for a single long line. Small output stays unchanged. Current AppPaths is resolved per capture. | Boundary, unicode, runtime root tests |
| O4 | Capture computes a full raw content digest before truncation and a separate stored digest. ToolResult metadata exposes capture facts without changing audit result_hash semantics. | Registry metadata and same-prefix different-tail tests |
| R1 | ToolRoundRunner owns tool schemas, policy/hooks/approval and one batch's messages. The model loop commits a completed ToolRoundResult; no caller conversation is passed to the runner. | Direct runner contract and strict provider tests |
| R2 | All tool results precede runtime notes, in call order; reasoning survives unchanged. Schema loader, render_ui, scoped capabilities and time notes keep their behavior. | Existing budget, UI, schema and protocol tests |
| R3 | Approval notification streams before waiting. Execute the exact suspended call once; guard recheck, scope, denial, timeout, hook block and cancellation remain effective. | Existing approval, runtime, scoped registry and cancellation tests |
| P1 | Progress identity includes normalized tool arguments and the full raw result digest, never call IDs or archive paths. Changing polling results count as progress. | Regression and progress unit tests |
| P2 | Three repeated unchanged observations warn once per retained pattern; alternating cycles of periods 2–5 repeated three times are recognized. Detection is bounded to a 30-observation window. | Repetition, cycle, eviction, canonical arguments tests |
| P3 | ProgressLedger reports typed decisions, without stopping execution. Existing failure and iteration budgets remain policy decisions; success resets consecutive identical failures. | Failure/reset tests and existing budget regressions |

## Design

`ToolOutputStore.capture(text, tool_name)` returns a frozen capture value: preview,
optional path, raw/stored digests, original size, and truncation flag. Exclusive
temporary-file creation supplies identity and permissions. Only oversized outputs
are archived. `truncate_output` remains a compatibility wrapper. Registry metadata
uses `output` as a reserved namespace; execution-ledger replay without capture
metadata interrupts progress observation: a bounded preview cannot establish full
result equality. Fresh results from registries without capture metadata use their
returned content digest. No execution-ledger migration is introduced.

`ToolRoundRunner` is created once per run with registry, ToolPolicy and optional
hooks. `run(calls)` is an async iterator yielding existing ExecutionEvents followed
by a frozen ToolRoundResult. Approval events are never buffered. Result messages
and notes are owned by the batch; terminal failures produce a terminal result and
the parent does not issue another model request. Execution remains serial.

`ProgressLedger` retains only compact fingerprints and bounded warning identity;
it never stores raw outputs. Its observations distinguish progress, repeat, cycle
and failure. The runner translates observations into existing recovery/warning
messages; it owns neither model context fitting nor the run iteration budgets.

## Explicit non-goals

No durable resumable conversation snapshots, automatic artifact retention, background
subtask scheduling, parallel tool execution, new approval policy or model adapter.
No platform/API/UI changes. Existing archives are not migrated or deleted. The
preview budget excludes the short retrieval hint, matching the existing API.

## Validation and rollback

Tests precede implementation. Preserve RED/GREEN checkpoint commits. Run targeted
behavior tests, engine/server regressions, and coverage (>=80% for new modules).
Audit installed Python dependencies before commits and review the final diff before
push. Tests use temporary AppPaths and mocked providers, without model credentials.
No data migration: revert the implementation commits to roll back.
