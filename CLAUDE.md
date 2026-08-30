# CLAUDE.md

This file is the working brief for Claude or any coding agent operating in this repository.

**Trust code over docs.** `docs/` has not been kept in sync with refactors. When
this file and the code disagree, the code wins — and fix this file. Count things
by the consumer's criterion, never by directory listing: `engine/tool/registry.py`
accepts a tool only if it defines `TOOL_META` + `execute`; a skill counts only if
its directory holds a top-level `SKILL.md`.

## 1. What This Project Is

Agent-Smith is a local-first personal assistant Agent workbench that runs in the terminal.

- Smith is the single, always-on Agent
- Smith uses the skill system to switch workflows per task type
- No multi-agent routing — one Agent, one conversation, accumulating memory over time
- Smith can delegate scoped work to **ephemeral sub-agents** (§6b) that run in
  isolation and return only a summary. They are a context-budget tool, not a
  second resident agent: they hold no memory, no session, and no profile record.

One-line:

> Agent-Smith is a local-first Agent workbench. Smith is your single resident
> assistant — it keeps context, accumulates memory, and switches workflows via skills.

## 2. Current Priority

The current priority is the terminal workbench experience:

1. Ink shell (`shell/`) is the sole entry point and provides the rich terminal UI
2. Skill-based workflow switching works for different task types
3. Memory accumulation across sessions

## 3. Agent Architecture

```
User ──▶ Ink shell ──HTTP + SSE──▶ server ──▶ engine
                                                │
        identity_catalog (agents/identities/smith.yaml · coding.yaml)
        keyword/example match (lexical only, no LLM) ──▶ RouteDecision
                                                │
                        ┌───────────────────────┴──────────────────┐
                        ▼                                          ▼
                 route: no pipeline                          route: pipeline
                 direct ReAct loop                  (agents/pipelines/*.yaml)
                                                                   │
   requirements-research:  grilling → research → ecc-plan
       gates: grilling_complete / research_brief / plan_confirmed
   tdd-development:  diagnosing-bugs* → tdd-workflow → verification-loop
       gates: red_loop / tdd_evidence / tdd_verification
       * runs only when `coding_bugfix_needs_diagnosis` holds
   code-review:  code-review → verification-loop
       gates: review_report / review_verification
                                                                   │
        gate fails ──▶ node retries with gate feedback (CTX_RETRY_HINT)
```

Declared routes: `agents/identities/coding.yaml` holds the three chain routes —
`requirements-research` (priority 30), `tdd-development` (20), `code-review`
(10); `agents/identities/smith.yaml` holds only `git` (no pipeline). Ordinary
coding, bugfix, and refactor requests are deliberately **not** hijacked by
keywords — they stay in generic ReAct. A pipeline node falls back to generic
ReAct when no matching `SKILL.md` is installed; the gate still runs, so the
intermediate contract stays observable.

Prompt assembly (`engine/context/assembler.py`) stacks 16 trust-tagged layers:
Agent Role / Style / Workflow, Tool Usage Policy, Available Tools, Available
Skills, Learned User Context, Global Instructions, Project Instructions,
Identity Guidance, Evaluation Safety Guidance (conditional), Output Style,
Memory Governance, Durable Memory, Runtime Context, Engine Runtime Control.

Memory is two rendered views and nothing else: `context.md` (user
collaboration) and `memory/durable.md` (project). Both are budget-capped by
`engine/memory/MEMORY_POLICY.md` and injected **whole** — there is no
query-time retrieval, no FTS index, no embeddings, and no episodes. Evidence
accumulates in `memory/recent.jsonl`; `compile_durable()` merges it
incrementally against `.compile_offset`, and Dream only sanitizes the rendered
files and reclaims the expired prefix of the event log.

The compiler emits a **change set**, never a document (`_changeset.py`), and
every change passes three deterministic guards (`_guards.py`, policy §6.1)
before the LLM reviewer sees it: traceability (`evidence.ref` real, `quote`
verbatim, falsifiable anchors inside the cited event), retention (a conclusion
is only erased by `forget`/`correction`), placement (`work` evidence cannot
establish a `Verified Outcomes` entry). Rejection is per change, so one bad edit
does not sink the batch, and the reviewer is shown only what survived. On total
failure nothing is written — a degraded draft would become the next round's
trusted baseline. `_snapshot.py` makes `~/.agent-smith/` a git repository and
commits `context.md`, `memory/durable.md` and `memory/recent.jsonl` after every
accepted write, so recovery is not limited to one `.bak` generation. The
evidence log is tracked with the views because restoring a conclusion without
the evidence it cites leaves the two out of step. `snapshot_baseline()` captures
the pre-compilation state once — the repository is created *after* the first
write, so nothing else can reach it — and costs a stat, not a git call, on every
later write. Dream snapshots on both sides of a reclaim; sanitize deliberately
snapshots only afterwards, because committing the pre-sanitize text would park
the leaked secret in git history permanently. `list_snapshots()` /
`restore_snapshot()` are the undo path, and a restore snapshots the current
state first, so aiming one at the wrong commit is itself recoverable.

Each view owns a log cursor (`.compile_offset` for durable, `.compile_offset_context`
for context) and advances it only past events that actually fit the 24k prompt
budget. Dream reclaims up to whichever cursor is further behind and rebases both.
After three consecutive `deferred` cycles (nothing applicable) the batch is
skipped — cursor only, still no write. `rejected` (unsafe/malformed draft) and
`failed` (provider outage) never count towards that, because neither is the
evidence's fault.

## 4. Product Language

Use: "Smith", "Agent", "skill", "session", "memory", "tool", "template",
"sub-agent" (an ephemeral, memoryless delegate — never a second resident Agent)

Avoid: "employee", "digital employee", "hire"

## 5. Architecture Boundaries

Four layers, one-way dependencies (verified by import graph, not by convention):

```
server/ → engine/ → common/
          ↑
        agents/   (loaded at runtime, never imported)
```

Plus two frontends: `shell/` (Ink/React, terminal) and `desktop/` (Electron,
DOM). They share **rendering decisions, not renderers** — Ink has no DOM
backend. `shell/src/theme.ts` (palette), `presentation.ts` (markers, welcome
art) and `diff-parse.ts` (unified-diff parsing) are Ink-free precisely so the
desktop can import them; a marker or colour must never be duplicated.

| Layer | Directory | Source lines (non-test) | Responsibility |
|---|---|---|---|
| Infrastructure | `common/` | 1.1k | Paths, SQLite connection, YAML read/write, hash chain. Zero business logic. |
| Execution | `engine/` | 25.3k | Agent framework: LLM, pipeline + ReAct, memory, skills, tools, safety, observability. Zero platform knowledge. |
| Content | `agents/` | 9.9k | Smith identity seed, pipelines, gates, conditions, tools, skills, hooks. Pure content. |
| Platform | `server/` | 6.1k | FastAPI. Orchestration, session/agent lifecycle, 35 HTTP endpoints (34 router + `/api/health`). |
| Terminal UI | `shell/` | 10.7k TS | Ink shell. Calls server over HTTP, auto-starts the backend. |
| Desktop UI | `desktop/` | 3.1k TS | Electron shell. Imports `shell/src` verbatim; main process runs shell's own `ensureLocalServer()`. |

Rules:

- `engine/` must not know FastAPI, HTTP, or agent instance management
- `agents/` gates, conditions, and tools import nothing from other layers — the
  tool registry loads its `.py` files via `exec_module`, so the contract is
  `TOOL_META` + `execute`, not types, and a path constant cannot be shared into
  them. The one sanctioned exception is `agents/smith/hooks/`: hooks implement
  the `engine.execution.hooks` ABCs and may read `common` for the data root.
  Both boundaries are test-enforced (`engine/tests/skill/test_skill_chain.py`).
- `server/app/routers/` stays thin — extract params, call service, return result
- `server/app/` is the FastAPI application package; keep this conventional layout
- `agents/smith/` is where Smith's built-in identity seed lives
- New capabilities → add skills, not new agents
- `engine/observability/` and `engine/execution/orchestration/run_state.py`
  share `~/.agent-smith/runs/`: the observability index holds `<id>.summary.json`,
  the state store holds `<id>.json`. Retention lives with observability but
  deletes both, so it goes through `RunStateStore.prune()` rather than building
  the path itself — the state store owns the filename *and* the refusal to
  delete a run that is still executing. A consequence worth knowing:
  `AGENT_SMITH_OBSERVABILITY_*` therefore also bounds how long a finished run
  stays resumable.

`common/paths.py` is the single source of truth for the runtime data root
(`~/.agent-smith`, created `0o700`/`0o600`; a pre-existing directory keeps the
mode the user gave it — deliberate, test-locked behavior). `engine/safety/tool_guard.py`
anchors its non-bypassable platform-write protection on it.

## 6. Files That Matter

| Area | Key Files |
|---|---|
| Terminal entry | `shell/bin/smith.js` → `shell/src/index.tsx` |
| Backend spawn | `shell/src/dev-server.ts` (runs `uv run uvicorn app.main:app`) |
| Engine assembly | `server/app/services/engine_runtime.py` |
| Agent lifecycle | `server/app/services/agent_profile_service.py` |
| Chat + execution | `server/app/services/session_service.py` |
| Run lifecycle | `engine/execution/orchestration/lifecycle.py` |
| Agent dispatch | `engine/execution/orchestration/agent_loop.py` |
| ReAct loop | `engine/execution/react/react_loop.py` |
| Task routing | `engine/execution/routing/task_router.py`, `engine/identity/catalog.py` |
| Pipeline + skill chain | `engine/execution/pipeline/pipeline.py`, `.../skill_chain.py` |
| Prompt assembly | `engine/context/assembler.py` |
| Tool policy | `engine/safety/tool_policy.py` (hard guard before soft challenge) |
| **Hook system** | `engine/execution/hooks/` (framework), `agents/smith/hooks/` (built-in implementations) |
| **Sub-agents** | `engine/execution/subagent/` (framework), `agents/subagents/*.yaml` (types), `agents/tools/sub_agent.py` (provider) |
| Data root | `common/paths.py` |
| Smith profile seed | `agents/smith/` |
| **End-to-end map** | `docs/04e-Engine-全链路白盒地图.md` — one turn from input to output, node by node, and what each node records |

## 6a. Hook System

Smith has a **three-layer Hook system** for tool execution lifecycle management:

### Architecture

```
PreToolUse (can block) → Tool Execution → PostToolUse (warnings only) → Stop (batch processing)
```

**Three Hook types**:

1. **PreToolHook** - Executes before tool call, can block dangerous operations
   - Priority-ordered (lower number = higher priority)
   - Returns `(allowed: bool, denial_reason: str | None)`
   - Use for: security enforcement, policy validation, configuration protection

2. **PostToolHook** - Executes after tool call, observes results
   - Cannot block (tool already executed)
   - Returns `list[str]` of warnings (injected into conversation)
   - Can be async (non-blocking)
   - Use for: quality checks, logging, metrics

3. **StopHook** - Executes at end of each Agent response
   - Typically async (non-blocking)
   - Use for: cost tracking, session persistence, batch processing

### File Layout

```
engine/execution/hooks/
├── tool/                # Tool lifecycle hooks
│   ├── interface.py     # PreToolHook / PostToolHook / StopHook ABCs
│   ├── manager.py       # HookRegistry (registration + execution)
│   └── loader.py        # HookLoader — dynamic loading from YAML config
├── extension/
│   └── manager.py       # HookManager/HookType — engine-internal extension hooks
└── __init__.py          # re-exports both systems under one import path

agents/smith/hooks/
├── config_protection.py # PreToolHook: Block config file modifications
├── console_warn.py      # PostToolHook: Warn about debug statements
├── cost_tracker.py      # StopHook: Track token usage and costs
├── quality_gate.py      # PostToolHook: Run format/lint checks
└── __init__.py

agents/smith/hooks.yaml  # Hook configuration (which hooks are enabled)
```

### Integration Points

- `preparation.py`: Loads hooks from `agents/smith/hooks.yaml`, then user hooks from
  `~/.agent-smith/hooks.yaml`, into `services.hook_registry`
- `react_loop.py`: Calls `hook_registry.run_pre_hooks()` before tool execution, `run_post_hooks()` after
- `lifecycle.py`: Calls `hook_registry.run_stop_hooks()` at response end

### Built-in Hooks

| Hook | Type | Enabled | Purpose |
|------|------|---------|---------|
| `config-protection` | Pre | ✅ | Block edits to linter/formatter/type-checker configs |
| `console-warn` | Post | ✅ | Warn about `console.log`, `print()`, etc. |
| `quality-gate` | Post | ✅ | Run format/lint checks (async) |
| `cost-tracker` | Stop | ✅ | Write token usage to `~/.agent-smith/metrics/costs.jsonl` |

`cost-tracker` bills the run's aggregated `TOKEN_USAGE` events against the
interactive client's model name. It writes nothing when a run reported no usage
at all — a setup failure that never reached the provider must not appear as a
zero-token call. `MODEL_PRICING` only carries Anthropic rates, so another
provider records `estimated_cost_usd: null` rather than a wrong number.

The fact gate (require investigation before the first edit) is **not** a
pluggable hook anymore: it lives at `engine/safety/fact_gate.py` and is wired
per request in `lifecycle.py` (`use_fact_gate`), always active, challenge-only.

### Extension

Users can add custom hooks:
1. Write a Hook class implementing `PreToolHook`, `PostToolHook`, or `StopHook`
2. Add entry to `~/.agent-smith/hooks.yaml` (loaded after built-in hooks)

Hook system is **pluggable** — engine provides framework, agents provide implementations.

## 6b. Sub-Agent System

Smith can hand a scoped task to a **sub-agent**: one isolated ReAct
conversation that runs to completion and returns only its final report. The
parent pays the tokens of a summary, not of a transcript. A sub-agent has no
memory, no session, no profile record, and cannot ask the user anything.

```
Smith ──sub_agent(tasks=[…])──▶ run_sub_agents()
                                    │  asyncio.gather, Semaphore(max_parallel)
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
              fresh ReAct     fresh ReAct     fresh ReAct
              ScopedToolRegistry per declared type, shared ToolGuard
                    └───────────────┼───────────────┘
                                    ▼
                        one rendered report ──▶ Smith's conversation
```

### Where each piece lives

| Concern | File |
|---|---|
| Type definitions (prompt, tools, model role, iteration + token caps) | `agents/subagents/*.yaml` |
| Type parsing + validation | `engine/execution/subagent/catalog.py` |
| Fan-out, isolation, failure containment | `engine/execution/subagent/runner.py` |
| Tool schema + report rendering | `agents/tools/sub_agent.py` |
| Capability injection | `bind_sub_agent_tool` in `.../orchestration/builtin_tools.py` |

The content layer keeps its zero-import boundary: `agents/tools/sub_agent.py`
imports nothing from `engine`, and the engine injects `_spawn` through
`wrap_tool` — the same pattern `memory_ops` and `skill_load` already use.
Injection happens *after* the model's arguments, so a model-supplied `_spawn`
cannot displace the real capability.

### Shipped types

| Type | Writes? | Purpose |
|---|---|---|
| `explorer` | no | Search and read the local tree; report with `path:line` |
| `reviewer` | no | Adversarial defect review; must construct a concrete failure |
| `implementer` | yes | One scoped change, verified by running something |

`reviewer` deliberately has **no** `git_ops`: scoping is per tool *name*, not
per action, and `git_ops` carries commit/push/branch alongside diff. The
parent — which knows which diff matters — puts it in the brief. `implementer`
deliberately has **no** `todo`: it is session-scoped and index-addressed, so
parallel siblings would overwrite each other's entries, and it is the user's
visible task list rather than scratch space.

All three ship on the default (`interactive`) port. A type may declare
`model: background` to run on the cheaper port instead — content names a
**role**, never a provider or model string, so credentials and model selection
stay in the operator's config. An absent port falls back to `interactive`
rather than failing the task.

### Invariants

- **No recursion.** `sub_agent` is stripped from every declared tool list at
  catalog load *and* excluded again when the `ScopedToolRegistry` is built. A
  sub-agent that tries to spawn one gets a `tool_disabled` result.
- **No privilege escalation.** `ScopedToolRegistry._active_names()` intersects
  with the parent registry, so a type cannot name a tool the identity or the
  profile config has disabled. The same `ToolGuard` covers every inner call.
- **Failure is contained.** One task raising, timing out, or naming an unknown
  type produces a failed *outcome*; siblings still complete. Only parent
  cancellation propagates.
- **A summary is the product.** A sub-agent that returns empty text is a
  failure, whatever the loop reported.
- **Ceilings are the engine's, not the content's.** 10 tasks per call, 8
  concurrent, 40 iterations, 400k tokens per agent, 600 k per batch, 600 s per
  task — a YAML author cannot raise them.
- **Spend is bounded twice.** `max_iters` bounds *turns*; `token_budget` bounds
  *tokens*, which is what a runaway with large tool results actually burns. A
  provider that omits `total_tokens` is charged input+output, so missing
  accounting cannot read as free. A provider that reports *no* usage at all —
  common on OpenAI-compatible relays that drop `stream_options.include_usage` —
  is charged an estimate synthesized from the fitted input plus the output
  text; without it neither budget applied. That estimate carries
  `usage_reported: 0`, and the token-stats store files it under an
  `estimate:` source key so a cost panel never shows a guess as billing data.
  Both budgets stop the loop between turns and report the partial state as a
  failure — the tokens are already spent, so the only useful response is to
  stop compounding them.
- **An uninstallable capability stays out of the prompt.** An absent or
  malformed `agents/subagents/` marks the tool `hidden` (logged at ERROR)
  rather than failing every turn.
- **No human in the loop.** Each sub-agent runs under
  `without_approval_context()`. Inheriting the parent's broker made the loop
  `await broker.wait()` on a prompt the user never sees — the runner discards
  sub-agent events — so it hung until the task timeout. Detached, the call is
  refused and the agent can adapt or report.
- **The fact gate is per sub-agent.** `FactGate` carries mutable per-turn
  state that `begin_round()` rewrites; sharing the parent's instance across a
  fan-out let one agent's round boundary satisfy a sibling's outstanding
  challenge. Each sub-agent gets its own gate, scoped to its own tool set.
- **Hooks apply to delegated work.** `hook_registry` is passed through, so
  `config-protection` and the other `PreToolHook`s cover a sub-agent's calls.
  Without it a sub-agent could edit files the parent is blocked from touching.
- **The report fits in a tool result.** The runtime truncates past 50 KB and
  spills the rest to a file, which would drop the *tail* — the last agents'
  findings. `agents/tools/sub_agent.py` budgets ~40 KB across however many
  agents ran, measured in **bytes**: a CJK report is ~3x its character count,
  and a character cap sailed past the ceiling.
- **The prompt names the tools.** A sub-agent's prompt is not built by
  `PromptAssembler`, so it has no "Available Tools" layer; under the runtime's
  lazy-schema mode the model sees only a schema *loader* and would otherwise
  have to guess names to load.
- **A read-only type holds no write-capable tool.** Enforced against
  `ToolDefinition.is_write_tool` / `permission_level`, not against the type's
  own prose — a description saying "read-only" proves nothing.
- **No type reaches session-shared or durable state.** `todo`, `memory_ops`,
  and `skill_manage` are barred from every shipped type.
- **The tool list is validated after the recursion strip.** Checking the
  declared list let `tools: [sub_agent]` pass as "at least one tool" and then
  yield a type with none.
- **Binding is idempotent.** Appending the catalogue unconditionally
  duplicated it in the tool's public description on every extra bind.
- **Parallel tasks must not overlap on files.** Concurrent edits are
  last-write-wins and nothing detects the clash; the tool description tells
  the parent to give each task a disjoint scope.

Tests: `engine/tests/execution/test_sub_agent.py`.

## 7. Smith Profile System

Only one built-in Smith identity exists. Its source files live in `agents/smith/`
(`role.md`, `style.md`, `workflow.md`, `context.md`, `toolbox.md`, `config.yaml`).
The legacy `personal-assistant` id remains only as a compatibility role/template
id (`SMITH_TEMPLATE_ID` in `engine/llm/model_config.py`, `role:` in
`agents/smith/config.yaml`) for existing API and data paths. Legacy multi-agent
templates have been removed; optional skills can still be installed into Smith's
runtime profile.

Skills that ship with Smith are mirrored into `~/.agent-smith/builtin/skills/`,
discovered by scanning `agents/skills/` for directories holding a `SKILL.md`.
A wheel install ships them through `[tool.setuptools.data-files]` in
`common/pyproject.toml`, which needs one entry per skill — a test asserts that
declaration stays in sync.

Profile seeding is copy-once: `init_smith_profile_files` skips any file that
already exists in `~/.agent-smith/`, so additions to the repo seed (e.g. the
commented `knowledge:` example in `agents/smith/config.yaml`) reach fresh
installs only. Existing installs configure features by editing
`~/.agent-smith/config.yaml` directly.

## 8. Implementation Guidance

- Inspect current code first; prefer existing patterns
- Keep changes local; preserve compatibility unless asked to break it
- New task workflows → `SKILL.md` files, not new agents
- Smith identity changes belong in `agents/smith/`; capabilities belong in skills
- Safety changes: `tool_guard.py` is the non-bypassable boundary, `fact_gate.py`
  only challenges and can be retried. Keep the guard first — a test enforces it.

### Working Rhythm

Intent routing decides between direct ReAct and one of the three shipped
skill chains (§3). Explicit entry points:

| Intent | Entry |
|---|---|
| Turn a vague ask into a decided plan | `grill me` → `grilling` → `research` → `ecc-plan` |
| Requirements research | keywords (`需求调研`, `调研`, `研究一下`, …) → `requirements-research` chain |
| TDD feature/bugfix | keywords (`tdd`, `测试驱动`, `先写测试`, …) → `tdd-development` chain |
| Review a diff before it lands | keywords (`code review`, `代码评审`, `评审一下`, …) → `code-review` chain |
| Everything else | stays in generic direct ReAct |

Routing is **lexical only** — `IdentityCatalog` keyword/example matching with
priority, via `route_task()`. There is no LLM fallback classifier: one existed,
but running it on every keyword miss slowed ordinary direct-ReAct turns and
could start a multi-step workflow the user never asked for, so a pipeline now
requires a declared, high-confidence intent. Routing cannot invent an identity,
domain, or pipeline. `grill me` stops at shared understanding — it does not
hand off or implement.

## 9. Testing And Verification

```bash
cd server && uv run --with pytest --with pytest-asyncio pytest
cd engine && uv run --with pytest --with pytest-asyncio pytest
cd shell && npm run build && npm test
cd server && uv run uvicorn app.main:app --port 8000
```

Current baseline (macOS): engine 1211 passed (0 skipped), server 247 passed
(5 skipped), shell 303 passed (not re-measured this change). The engine's
Seatbelt skips appear on Linux only; every one carries
`@pytest.mark.skipif(sys.platform != "darwin")`, so on macOS they run instead.
A Seatbelt test *failing* rather than skipping on Linux means that marker is
missing — add it.

`shell` has 12 auth-dependent tests that fail on a container with no
`~/.agent-smith/auth_token`: they call the real `localAuthHeaders`, which reads
that file. Create it (`mkdir -p ~/.agent-smith && printf token > ~/.agent-smith/auth_token
&& chmod 600 ~/.agent-smith/auth_token`) to run the whole suite green, or treat
them as environment noise — they fail identically on `main`, not as a regression
from your change.

## 10. Not Implemented Yet

Product intent recorded here so it is not mistaken for existing behavior:

- **Knowledge injection.** Earlier drafts of this file described injecting
  domain expertise (frontend/backend knowledge docs) on demand. No such
  mechanism exists. `agent_profiles.knowledge` is a `list[str]` column that the
  prompt assembler does not read.

## 11. Default Decision Rule

If a choice is unclear, prefer the option that:

- makes the single-Agent terminal experience more usable
- reuses existing skill infrastructure
- avoids introducing multi-agent *routing* complexity (delegation via §6b
  sub-agents is fine; a second resident Agent is not)
- keeps changes minimal and reversible
