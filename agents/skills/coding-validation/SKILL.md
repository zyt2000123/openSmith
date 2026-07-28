---
name: coding-validation
description: Execute and report the smallest sufficient verification for a coding change, with real commands, outputs, failures, and residual risk.
version: "1.0.0"
---

# Coding Validation

## Use When

This is the final stage of the Coding Agent workflow. It turns a proposed
patch into a truthful delivery result.

## Steps

1. Select tests and static checks that prove each acceptance criterion and changed interface.
   Completion: the choice is tied to the implementation plan, not a generic command list.
2. Run the checks in the actual workspace.
   Completion: capture the exact command, relevant output, and pass/fail count or exit result.
3. Inspect the resulting diff and identify unverified paths or blocked checks.
   Completion: the final status separates passed evidence, failed evidence, and residual risk.

## Deliverable

Use `验证命令`, `结果`, `覆盖范围`, and `剩余风险`. Include actual execution
evidence such as `3 passed`, a compiler exit status, or an explicit failure;
never substitute an intention to test for a result.

## Safety

If a command needs credentials, production access, or a destructive action,
stop and request the appropriate authorization instead of simulating success.
