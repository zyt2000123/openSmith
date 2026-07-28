---
name: coding-implementation
description: Implement a coding plan as a minimal, reviewable patch while preserving stated interfaces, safety constraints, and traceability to plan items.
version: "1.0.0"
---

# Coding Implementation

## Use When

This stage applies the approved coding plan after understanding, planning, and
when needed architecture design have completed.

## Steps

1. Re-read each planned file and its adjacent tests before editing.
   Completion: the proposed change follows local conventions and does not overwrite unrelated work.
2. Implement the smallest coherent patch, mapping every edit to a numbered plan item.
   Completion: the output cites changed files and states `一致` or an explicit approved deviation for every plan item.
3. Add or update the narrowest relevant tests together with the behavior change.
   Completion: there is an executable verification target for the validation stage.

## Safety

Do not stage, commit, push, delete material data, or bypass approvals unless
the user explicitly authorized that operation. Do not describe an edit as
verified until a validation command has actually run.

## Deliverable

Report `实现`, `计划对齐`, `修改文件`, and `待验证`. The final validation
stage owns the pass/fail claim.
