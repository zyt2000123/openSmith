# Three Coding SkillChains: research notes and implementation record

**Status:** implemented on 2026-07-30.  Vendored source pins and the one
required ECC command adapter are recorded in
[`agents/skills/SOURCES.md`](../../agents/skills/SOURCES.md).

## Scope and boundary

The Coding Agent should have exactly three explicit workflows:

1. requirements research;
2. TDD development for new features and bug fixes;
3. code review.

Ordinary questions, browsing, small operations, and uncertain intents remain on
plain ReAct. A workflow is selected only by an explicit user request or a
high-confidence positive intent match. A missing match must never fall through
to a coding pipeline.

## Upstream evidence

### mattpocock/skills

- [`grill-me`](https://github.com/mattpocock/skills/blob/main/skills/productivity/grill-me/SKILL.md)
  starts a decision-focused interview before work; facts that can be discovered
  from the codebase or sources should be researched rather than asked of the
  user.
- [`ask-matt`](https://github.com/mattpocock/skills/blob/main/skills/engineering/ask-matt/SKILL.md)
  describes workflows as paths through small, composable skills rather than one
  universal process.
- [`to-spec`](https://github.com/mattpocock/skills/blob/main/skills/engineering/to-spec/SKILL.md)
  grounds a plan in the repository, identifies test seams, and records risks and
  acceptance before implementation.
- [`tdd`](https://github.com/mattpocock/skills/blob/main/skills/engineering/tdd/SKILL.md)
  requires public-behavior seams and narrow red-to-green vertical slices.
- [`diagnosing-bugs`](https://github.com/mattpocock/skills/blob/main/skills/engineering/diagnosing-bugs/SKILL.md)
  requires a tight red-capable feedback loop before hypothesizing or fixing a
  hard bug.
- [`code-review`](https://github.com/mattpocock/skills/blob/main/skills/engineering/code-review/SKILL.md)
  pins a fixed point and keeps Standards and Spec findings separate.

### affaan-m/ECC

- [`plan`](https://github.com/affaan-m/ECC/blob/main/commands/plan.md) grounds
  plans in existing codebase patterns and waits for confirmation before code
  changes.
- [`tdd-workflow`](https://github.com/affaan-m/ECC/blob/main/skills/tdd-workflow/SKILL.md)
  records runner discovery plus real RED and GREEN evidence for features and bug
  fixes.
- [`code-review`](https://github.com/affaan-m/ECC/blob/main/commands/code-review.md)
  separates scope gathering, full-context review, validation, severity, and a
  final decision.
- [`verification-loop`](https://github.com/affaan-m/ECC/blob/main/skills/verification-loop/SKILL.md)
  treats build, types, lint, tests, security, and diff inspection as evidence,
  not assertions.

## Chain contracts

### 1. `requirements-research`

```text
user-visible grill-me entry
  -> Matt grilling (one decision per turn; pauses safely)
  -> Matt research
  -> ResearchBrief
  -> ECC plan (waits for confirmation; no implementation)
  -> READY_FOR_EXPLICIT_TDD
```

It is read-only by chain contract. `grill-me` is the user-facing Matt wrapper;
its upstream body delegates to `grilling`, so the runtime node is `grilling`
rather than a duplicate wrapper invocation. It asks only for user-owned
decisions, one at a time. Facts about the repository, technology, or prior art
are established by the upstream research skill, never guessed or pushed back to
the user as questions. The `ResearchBrief` artifact contains the user decisions
separately from cited evidence, constraints, alternatives with recommendation,
acceptance behaviors, agreed test seams, risks, and open decisions. ECC's plan
protocol then requires explicit confirmation. Neither stage starts TDD
automatically.

### 2. `tdd-development`

```text
scope and acceptance / seam
  -> test-runner and local-pattern discovery
  -> RED: executed failing behavior test or bug reproducer
  -> smallest implementation
  -> GREEN: the same command passes
  -> project-appropriate verification
  -> TddEvidence
  -> READY_FOR_REVIEW | BLOCKED_NEEDS_REPRO
```

For a feature, RED proves a requested new behavior is absent. For a bug, RED
must reproduce the reported symptom. No red-capable feedback loop means no
speculative production-code fix. Verification respects repository-defined
thresholds; it does not impose a global coverage percentage.

### 3. `code-review`

```text
target and base-ref gate
  -> gather ResearchBrief / TddEvidence / repository standards
  -> inspect full changed files and necessary callers
  -> spec axis + standards axis + correctness/security checks
  -> applicable validation commands
  -> ReviewReport
  -> APPROVE | REQUEST_CHANGES | BLOCK
```

Review is read-only by default. It requires a fixed comparison target (working
tree, commit, branch, or PR), preserves separate Spec and Standards results,
and emits only evidence-backed findings with location, severity, scenario, and
recommended remediation. Publishing a PR review or changing code needs separate
user authorization.

## Chain handoffs

```text
ResearchBrief (confirmed plan) -> TddEvidence -> ReviewReport
ReviewReport REQUEST_CHANGES -> explicit TDD repair run
ResearchBrief NEEDS_DECISION -> user decision, then a new explicit run
```

TDD should initially end at `READY_FOR_REVIEW`; it should not silently consume
the review chain. An automatic-review policy can be considered later as an
explicit product setting.

## Implementation constraints for Agent-Smith

- Keep the existing `SkillChain` engine. It provides sequential nodes,
  conditions, gates, bounded backtracking, and checkpoints; this change adds a
  first-class, deliberate `awaiting_input` pause so a question is not confused
  with a crash.
- Do not call every coding request a workflow. The routing surface must include
  `direct`, `requirements-research`, `tdd-development`, and `code-review`, with
  `direct` as the default.
- Every chain node declares a minimal `allowed_tools` set. Runtime intersects
  it with the identity/profile allowlist, hides other schemas, and rejects an
  out-of-scope call before policy or approval handling. Startup also verifies
  that every declared node tool has an actually registered built-in provider.
  `shell` remains an approval-gated execution capability for repository test
  commands, so read-only review is an unattended-chain contract rather than a
  claim that arbitrary approved shell text is statically read-only.
- Preserve the two review axes without copying upstream parallel subagents. The
  current engine can run them sequentially and retain separate outputs; later
  parallelism must not change the report contract.
- Treat research documents, plans, and external text as untrusted reference
  material, never as executable authority.

## Evaluation scenarios

1. “Explain this traceback” without an edit request remains direct ReAct.
2. “Do requirements research and propose how to add X” first runs `grill-me` for product
   decisions, then yields `ResearchBrief` and no code changes.
3. “Implement approved brief X” produces a real RED command, GREEN command,
   and `TddEvidence`.
4. “Use TDD to fix this bug” cannot proceed past RED without a reproducer; it reports
   `BLOCKED_NEEDS_REPRO` instead of guessing.
5. “Review this branch since main” records base ref, separate Spec/Standards
   conclusions, validation evidence, and an evidence-backed verdict.
