# PRD — agents/tools hardening (2026-07-30)

Status: **Phase 1 landed and verified · Phase 2/3 open**

Phase 1 result: R-01 through R-07 implemented, every acceptance criterion in
section 5 executed and passing, and each fix reverse-verified against its
pre-fix logic (restored in memory) to prove the behavior actually changed:

```
OLD git_ops files=['*'] -> COMMITTED {'src/.env', 'ok.txt'}
NEW git_ops files=['*'] -> BLOCKED
OLD grep 'cat|dog' -> NO MATCHES (wrong);  '[' -> silently no-match (wrong)
NEW grep 'cat|dog' -> MATCHED;  '[' -> Error
```

Regression suite after Phase 1: engine **800 passed** (from 796), server
**161 passed / 5 skipped**. Tests for R-01, R-02, R-03, R-04 and R-07 live in
`engine/tests/tool/test_tool_design_fixes.py`. R-05 and R-06 are verified by the
acceptance script but do not yet have committed regression tests — carried into
Phase 2 as **R-24**.
Scope: the 18 tool providers in `agents/tools/`
Method: 5 parallel `ecc` review agents, each required to execute a reproduction
before a finding counted; then independent re-reproduction of every P0/P1 by the
integrator.

## 1. Why this document exists

An earlier pass this same day found and fixed 6 defects. That pass was
finding-driven: each defect was patched as it was identified. This PRD exists
because that approach missed adjacent instances of the same defect class — most
visibly, the `git_ops` commit fix repaired the implicit (no-`files`) staging
branch and left the explicit `files=[...]` branch carrying **two** worse holes.

So: audit first, across the whole surface, with a reproduction per claim. Then
write down requirements. Then fix. No fix lands without a named requirement here.

## 2. Method and its limits

| Lens | Agent | Files |
|---|---|---|
| Network + command execution | `ecc:security-reviewer` | web_fetch, web_crawl, web_search, shell, git_ops |
| Filesystem | `ecc:security-reviewer` | read_file, write_file, edit_file, list_dir, glob_files, grep |
| State / skill / presentation | `ecc:code-reviewer` | memory_ops, todo, skill_load, skill_manage, render_ui |
| Silent failures | `ecc:silent-failure-hunter` | all 18 |
| PDF pair + adversarial re-review of the 6 earlier fixes | `ecc:python-reviewer` | **INTERRUPTED — did not run** |

**Known coverage gap.** The fifth lens never completed. Two consequences:
`read_pdf` / `render_pdf_page` received only the silent-failure lens, and the 6
fixes from the earlier pass got no dedicated adversarial re-review. One
regression among them (T-08) was caught incidentally by the silent-failure lens;
there may be others. **Re-running this lens is a Phase 3 requirement (R-22).**

Every agent was briefed on the repository's standing rule: judge by effect, not
by whether a keyword appears in the provider file, because protections here are
often implemented centrally in `engine/safety/tool_guard.py` or
`engine/tool/registry.py`. That rule held up — the false-positive rate was low,
and each lens also reported what it checked and found clean.

## 3. How a provider signals failure (needed to read section 4)

`engine/tool/registry.py::_looks_like_tool_error` treats a provider result as an
error **only** if the string starts with `Error:`, `HTTP Error:`, `URL Error:`,
`[BLOCKED]`, `Memory rejected:`, or carries an `[exit_code=N]` prefix with
`N != 0`. Anything else is recorded as success — to the ReAct loop and to the
side-effect ledger. A provider that reports a real failure in different wording,
or returns partial data shaped like complete data, is therefore not a style
problem. It is a correctness defect with a concrete consequence.

## 4. Findings

Status legend: **C-self** = reproduced by the integrator independently;
**C-agent** = reproduced by the audit agent with real output pasted;
**U** = suspected, not reproduced; **D** = downgraded, rationale given.

### P0

| ID | File | Defect | Status |
|---|---|---|---|
| T-01 | `git_ops.py:234` | `files=[...]` is scanned as literal strings but handed to `git add -- <files>`, where git expands it as a **pathspec**. `files=["*"]`, `["."]` or `["src/"]` stages everything it expands to, none of it scanned. | C-self |
| T-02 | `git_ops.py:234,271` | The explicit-`files` branch never scans the **pre-existing index**, but `git commit -m` commits the whole index. A secret staged by any earlier action is committed by the next narrow-looking commit call. | C-self |

Reproduction (both, verbatim output):

```
P0-1 files=['*']      -> COMMITTED {'.env', 'ok.txt'}
P0-2 pre-staged .env  -> COMMITTED {'.env', 'ok.txt'}
```

Not compensated centrally: `files` is a `list_path_args` entry, so
`FileGuard.check_path` validates each literal string as a boundary path. `"*"`
resolves to a literal file named `*` inside the workspace — inside the allowed
root, matching no sensitive-name pattern. The guard has no model of git pathspec
expansion or of index state.

### P1

| ID | File | Defect | Status |
|---|---|---|---|
| T-03 | `grep.py:71` | The ripgrep-absent fallback never passes `-E`. BSD grep defaults to POSIX BRE, where `\|` is a literal, so `cat\|dog` returns **"No matches found"** against a corpus containing both. `rg` is not on PATH on the reference machine, so this is the **live** path, not the fallback. | C-self |
| T-04 | `grep.py:84` | `subprocess.run`'s return code is never inspected; only stdout is. An invalid pattern (`[`, rc=2) returns the same `No matches found for: [` as a genuine zero-match search. | C-self |
| T-05 | `edit_file.py:78`, `write_file.py:69` | The injected `_snapshot_tracker` call is wrapped in `except Exception: pass`. A failed pre-edit backup lets the write proceed and report `OK:`; undo is impossible and no log records it — neither provider nor `engine/tool/snapshot.py` imports `logging`. The boolean return of `track()` is also ignored. | C-agent |
| T-06 | `web_crawl.py:504,679` | Per-page fetch failures accumulate into `warnings` and the crawl returns success-shaped output even at **zero pages retrieved**; `side_effect="write"` makes the ledger record `completed`. `output_format="json"` carries no natural-language cue at all. | C-agent |

Reproduction of T-03 / T-04 (verbatim):

```
has_rg: False
'cat|dog'      -> No matches found for: cat|dog
'cat'          -> # grep : 1 results  (corpus does contain both lines)
'[' (invalid)  -> No matches found for: [
```

T-03 is the most consequential finding in this audit after the P0s: any caller
searching a combined pattern — including a security review searching
`AKIA[0-9A-Z]{16}|sk-[a-zA-Z0-9]{32}` — receives a confident, silent
"nothing found."

### P2

| ID | File | Defect | Status |
|---|---|---|---|
| T-07 | `glob_files.py:80` | A pattern of many chained `**` segments recurses once per token during **pattern parsing**, before any filesystem I/O. ~8 KB of input raises an uncaught `RecursionError` from a tool whose `approval_policy` is `never`. | C-self |
| T-08 | `render_pdf_page.py:51,90` | **Regression introduced by the earlier pass.** The process-global render dir plus a filename derived from `sha256(path, page, dpi)` means the digest ignores file content/mtime, and a **failed** re-render leaves the previous successful PNG intact at the identical path. | C-agent |
| T-09 | `skill_manage.py:360` | `patch` calls `store.save_version()` before knowing the patch will apply. Eleven failed patches evict the genuine pre-edit version from the 10-slot history, defeating `rollback`. | C-agent |
| T-10 | `skill_manage.py:177` | `_patch_section` is fenced-code-block-blind. A heading-like line inside an example block ends the replace scope early, leaving duplicate headings, an unclosed fence, and un-replaced old content — while returning `OK: patched section ...`. | C-agent |
| T-11 | `list_dir.py:77` | `os.path.getsize` is uncaught; one file vanishing between `listdir` and `getsize` aborts the **entire** listing with no partial output. | C-agent |
| T-12 | `git_ops.py:324` | `discover` appends each section only `if rc == 0`. In a repo with no commits, `git log` genuinely fails (rc=128) and its section silently disappears; the result looks like a complete, healthy discovery. | C-agent |
| T-13 | `web_search.py:126` | A scraper miss (markup change, soft block without the `anomaly`/`captcha` keywords) is indistinguishable from a genuine zero-result search. | C-agent |
| T-14 | `read_pdf.py:108,174` | Per-page extraction failures become inline `[text extraction failed: ...]` text. If every page fails on both backends the result is still success-shaped. | C-agent |
| T-15 | `memory_ops.py:379` | A `remove_episode_from_index` failure returns a string starting with `OK:`. Mitigating: `engine/memory/store.py:199` does reconcile on the next compile, so "self-heal" is real — but a dangling index entry persists until then. | C-agent |
| T-16 | `write_file.py:39`, `edit_file.py:38` | The provider-level `_is_within_workdir` check is **dead in production** — `_work_dir` is set nowhere outside tests — and, being read from the same model-supplied `arguments` dict, is fully attacker-controllable if ever exercised. Not live-exploitable today (`ToolGuard` is the real boundary and was confirmed intact), but the codebase already defends this exact class for `environment` and `_snapshot_tracker` and not for `_work_dir`. | C-agent |

### P3

| ID | File | Defect | Status |
|---|---|---|---|
| T-17 | `read_file.py:67,92` | A single line over the 50 KB budget is cut mid-token with `errors="replace"`; the header reports the range as though the line ended there. | C-agent |
| T-18 | `list_dir.py:56` | A `PermissionError` on a subdirectory renders it as an empty folder, indistinguishable from genuinely empty. | C-agent |
| T-19 | `grep.py:35` | `--exclude-dir` matching is case-sensitive, so a directory spelled `.Git` is not excluded — inconsistent with this codebase's own `_casefolded()` discipline in `tool_guard.py`. | C-agent |
| T-20 | `glob_files.py:134` | `MAX_RESULTS` slices the response after full enumeration; traversal has no cap and no timeout, unlike `grep`'s 30 s subprocess timeout. | C-agent |
| T-21 | `memory_ops.py:97` | `mkdir(mode=0o700)` applies only to the leaf; `parents=True` ancestors get the process umask. Masked in the default topology because `common/paths.py::ensure_base_dirs` pre-creates the chain — but the same flawed helper is now duplicated in two files. | C-agent |

### Unconfirmed — no fix until reproduced

| ID | Claim | Why not confirmed |
|---|---|---|
| U-01 | `memory_ops` declares `concurrency: serial`, but the lock is a per-`ToolRegistry`-instance `asyncio.Lock` and a registry is built per request, so concurrent sessions sharing one `memory_dir` are not serialized. | Code walk only. O_APPEND of a short line is usually atomic, so end-to-end corruption was never demonstrated. |
| U-02 | `render_pdf_page` lacks `concurrency: serial`; concurrent identical digests could race on one output path. | No reliable timing reproduction. |
| U-03 | `web_search` resolves its fixed endpoint without `web_fetch`'s post-resolution IP-range validation. | Requires DNS control or TLS bypass to exploit. |
| U-04 | `web_crawl`'s Playwright `context.route("**/*")` may not intercept WebSocket connections. | `playwright` not installed; render path fails closed here. |
| U-05 | TOCTOU between the guard's `resolve()` and the provider's `open()`. | Requires true concurrency; every static symlink variant was correctly blocked. |
| U-06 | `memory_ops` follows a symlinked `mem_dir` and chmods the target to 0700. | Requires a same-privilege attacker to pre-plant the link; not privilege escalation. |

### Downgraded

**D-01 — `skill_load` returns skill content without secret/injection filtering.**
The auditing agent rated this P1 by analogy to `memory_ops`, which does call
`contains_secret` / `contains_injection` / `sanitize_memory_text`. The analogy
does not hold: memory is written by the model from tool output — i.e. from
untrusted external content — whereas a skill is user-installed configuration in
the same trust tier as `CLAUDE.md` and project instructions, and every mutating
`skill_manage` action is approval-gated. Filtering trusted configuration would
be the wrong boundary. **Decision: document the trust tier, do not filter.**
Revisit if an auto-install or marketplace path is ever added, which would move
skills out of the user-authored tier.

## 5. Requirements

Each requirement names its finding, the behavior change, and a runnable
acceptance criterion. A fix without a passing acceptance test does not count.

### Phase 1 — P0 + P1 + the self-inflicted regression

**R-01 (T-01, T-02) — `git_ops` commit must scan what the commit will actually contain.**
Preflight before any staging, so a refusal has no side effects. The scanned set
is the union of (a) the already-staged index —
`diff --name-only --diff-filter=ACMR --cached` — and (b) the exact paths staging
would add, obtained by expanding the pathspec with
`git -c core.quotePath=false add --dry-run [-- <files> | -u]` and parsing its
`add '<path>'` lines. A line beginning `add ` that does not parse must be
treated as a candidate path (fail closed).
*Accept:* `files=["*"]` with a nested `.env` is refused; a pre-staged `.env` is
refused even when `files=["ok.txt"]`; a legitimate commit with an untracked,
non-gitignored `.env` still succeeds; the index is unchanged after a refusal.

**R-02 (T-03) — `grep` must use one regex dialect and say which.**
Pass `-E` on the BSD/GNU `grep` fallback so alternation and `+`/`?` behave as
the tool's own "regex supported" description implies, matching ripgrep's
semantics closely enough for the documented use. Report the engine in the header.
*Accept:* `cat|dog` matches both lines on the fallback path; the header names the
engine used.

**R-03 (T-04) — `grep` must distinguish "no match" from "search failed".**
Inspect the return code: 0 = matches, 1 = genuinely no matches, >=2 = failure,
returned as an `Error:` string carrying stderr.
*Accept:* pattern `[` returns a string starting with `Error:`; pattern
`definitely-absent-xyz` returns the ordinary no-match message.

**R-04 (T-05) — a failed pre-edit snapshot must not be silent.**
Surface it. Do not abort the write (the caller asked for it and the engine owns
undo policy), but the result string must state that no undo record exists, and
the failure must be logged.
*Accept:* with the snapshot backup directory unwritable, `edit_file` still edits
but its result mentions the missing undo record.

**R-05 (T-06) — `web_crawl` must not report a zero-page crawl as success.**
When no page was retrieved, return an `Error:` string that includes the
collected warnings, in both output formats.
*Accept:* all fetches failing yields a result for which
`_looks_like_tool_error` is `True`, for `output_format` `markdown` and `json`.

**R-06 (T-08) — `render_pdf_page` must never hand back a stale image.**
Remove the failure window: delete any existing artifact at the target path
before invoking Poppler, and include file identity (size + mtime) in the digest
so an edited PDF cannot reuse an older render.
*Accept:* render page 3 of a 3-page PDF; replace the file with a 1-page PDF;
re-request page 3 -> error returned **and** no PNG remains at that path. Editing
a PDF in place and re-rendering the same page yields a fresh image.

**R-07 (T-07) — `glob_files` must reject a pathological pattern, not crash.**
Bound the component count and collapse consecutive `**` segments; return an
`Error:` string rather than raising.
*Accept:* a 2000-segment `**` pattern returns a string starting with `Error:`;
ordinary patterns including a single `**` are unaffected.

### Phase 2 — P2

R-08 (T-09) save the version only after the patch applies · R-09 (T-10) make
`_patch_section` fence-aware · R-10 (T-11) skip a vanished entry instead of
aborting the listing · R-11 (T-12) surface a failed `discover` sub-command ·
R-12 (T-13) distinguish a scraper miss from zero results · R-13 (T-14) fail when
no page yielded text · R-14 (T-15) stop prefixing an index-update failure with
`OK:` · R-15 (T-16) remove the dead `_work_dir` parameter or have the runtime
inject it the way `environment` is injected.

### Phase 3 — P3 and follow-up

R-16..R-21 for T-17..T-21 (one each). **R-22: re-run the interrupted
`ecc:python-reviewer` lens** over the PDF pair and over every fix landed by this
PRD and the earlier pass. **R-23: reproduce or retire U-01 and U-02** — no fix
lands on either while it is unreproduced.

## 6. Verification

Baseline before this PRD: engine 796 passed, server 161 passed / 5 skipped.
Every requirement above adds a regression test that **fails against the
pre-fix code** — a test that passes both before and after is not evidence.
