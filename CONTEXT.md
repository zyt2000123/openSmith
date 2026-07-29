# Agent-Smith ubiquitous terms

This glossary defines the product language for a resident local terminal Agent.
It is intentionally implementation-independent.

## Access request

An operation proposed by the model that expands beyond the session's initial
workspace grant or uses a sensitive/high-risk host capability: a file or
directory, a shell process, a network destination, or an external side effect.
An access request is visible to the user before it runs.

## Approval capability

A one-time authorization granted by the user for one exact access request. It
is bound to the normalized request and expires when that request completes; it
does not authorize a later, similar request.

## High-risk approval

An approval capability whose requested operation may expose credentials,
destroy or modify data, bypass ordinary guardrails, elevate privileges, or
reach the network. The approval UI must describe that risk rather than silently
turning it into a denial.

## Non-delegable runtime secret

A provider credential or other secret owned by the Agent-Smith runtime itself,
not by the task the user asked the model to perform. The model cannot obtain or
use it through an approval capability.

## Technical execution error

An error that makes an operation impossible to execute safely or faithfully,
such as an invalid path, an unavailable execution backend, or an unresolvable
path alias. It is not a policy refusal and must identify the failed execution
condition.
