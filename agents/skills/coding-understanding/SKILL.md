---
name: coding-understanding
description: Establish the evidence-backed goal, scope, constraints, and acceptance criteria before a software change enters the coding workflow.
version: "1.0.0"
---

# Coding Understanding

## Use When

This is the first stage of the Coding Agent workflow. It turns a feature,
bugfix, or refactor request into a bounded engineering objective before any
file is changed.

## Steps

1. Restate the requested outcome in concrete product and code terms.
   Completion: the output names the expected behavior, not only an implementation idea.
2. Inspect the smallest relevant runtime path, tests, configuration, or error evidence.
   Completion: the output cites the inspected files, commands, logs, or explicitly states what evidence is unavailable.
3. Record scope, constraints, risks, and acceptance criteria.
   Completion: at least two boundaries or constraints are explicit, including what must not change.

## Deliverable

Use the headings `目标`, `证据`, `范围与约束`, and `验收标准`. Do not edit
files or claim a root cause before the evidence supports it.

## Verification

The next stage can turn this into a plan without guessing the goal, scope, or
definition of done.
