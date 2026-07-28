---
name: coding-planning
description: Convert a verified coding objective into a small, ordered implementation plan with file-level changes and executable verification points.
version: "1.0.0"
---

# Coding Planning

## Use When

This is the second stage of the Coding Agent workflow, after the task goal and
boundaries have been established.

## Steps

1. Map each acceptance criterion to one or more implementation steps.
   Completion: no criterion depends on an unstated change.
2. Identify affected files, interfaces, migrations, and compatibility risks.
   Completion: each step names its likely code surface or says why no file change is needed.
3. Define verification immediately after the step it proves.
   Completion: every step has a concrete command, test, inspection, or manual check.

## Deliverable

Write at least three numbered steps. Each step must include `修改` and `验证`.
Call out when the plan crosses three or more code files so the architecture
stage can make the dependency flow explicit.

## Verification

The implementation stage must be able to compare its completed work against
these numbered items one by one.
