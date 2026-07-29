# ADR 0001: Gate host capabilities with per-request approval

## Status

Accepted — 2026-07-29

## Context

Agent-Smith is intended to be a resident terminal Agent on the user's
computer. A workspace-only hard deny makes normal local workflows impossible:
the user cannot approve a neighbouring project, a Downloads folder, a
credential file they own, a destructive command, or a network operation.

At the same time, a standing host-wide permission would make a later model
call able to act beyond the user's immediate intent.

## Decision

1. The working directory explicitly entered by the user is the session's
   initial narrow grant. Every model request that expands beyond it, or uses
   sensitive user data, a dangerous command, or network access, is an approval
   request.
2. A granted approval creates a one-time capability for the exact normalized
   tool call. It cannot be replayed for a changed target, command, or call id.
3. The execution backend receives only the capability needed by that approved
   call. In particular, an approved shell command gets a dynamic host scope;
   an unapproved shell remains workspace-confined.
4. Agent-Smith runtime provider/API credentials remain non-delegable. No model
   approval can expose them to a tool or child process.
5. Direct rejections are reserved for technical execution errors, not for the
   user's ownership of a resource. A failed guard must say whether approval is
   needed or the operation is technically unavailable.

## Consequences

- Approval cards must display the exact target or command and the requested
  access scope.
- Tool execution must re-check both the normalized call and its approval
  capability as a backstop.
- macOS Seatbelt profiles need two modes: workspace confinement by default and
  one-shot host access after a matching approval. Both modes continue to deny
  runtime-secret paths and inherited service credentials.
- Existing blacklist rules become high-risk approval classifiers unless they
  identify a non-delegable runtime secret or a technical impossibility.

## Rejected alternatives

- **Workspace-only hard deny:** safe but incompatible with a resident local
  Agent and requires manual file copying for ordinary tasks.
- **Permanent directory whitelist:** easier to implement but grants a broad,
  replayable authority that is not tied to the user's current intent.
- **Unrestricted host shell after any approval:** makes the approval card lie;
  the actual process authority would exceed the displayed request.
