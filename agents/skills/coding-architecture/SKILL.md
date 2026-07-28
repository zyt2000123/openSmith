---
name: coding-architecture
description: Design the dependency-aware change path for a multi-file coding task before implementation begins.
version: "1.0.0"
---

# Coding Architecture

## Use When

This stage runs only when the plan spans multiple code surfaces. It protects
the implementation from making local edits that violate an existing runtime
or data-flow contract.

## Steps

1. List affected files and the responsibility each one owns.
   Completion: ownership is explicit; no change is assigned only to a vague layer.
2. Trace the relevant request, data, event, or state flow from input to observable result.
   Completion: the flow names the handoffs and their invariants.
3. State dependencies, compatibility constraints, and the smallest viable design.
   Completion: the output identifies reuse points and the validation consequence of each interface change.

## Deliverable

Use `涉及文件`, `数据流`, `依赖与兼容性`, and `设计决策` headings. Cite the
plan items the design realizes. This stage proposes a design; it does not
claim implementation or tests have already completed.

## Verification

Another engineer can compare the following code changes with the declared
data flow and identify an unintended coupling.
