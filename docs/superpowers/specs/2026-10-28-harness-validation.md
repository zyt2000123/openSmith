# Harness verification record

Specification: [Harness SDD](2026-10-28-harness-design.md).
Base: `f5bcafc751a583a850d6a639d7e3cc6fe6ed489b` (`main`).
Environment: macOS, Python 3.11.14, pytest 8.4.2. Verified 2026-10-28.

## Results

| Check | Result |
|---|---|
| Initial acceptance RED checkpoint | 33 expected failures, 4 passes |
| Initial implementation GREEN checkpoint | Same 37 tests passed |
| Final acceptance contracts | 43 passed |
| Complete engine suite | 1,263 passed in 35.59 seconds |
| Complete server suite | 255 passed, 1 failed, 5 skipped |
| Unmodified main server comparison | Same 255 passed, 1 failed, 5 skipped |
| Isolated server timing test | Passed |
| Diff whitespace validation | Passed |

Additional RED/GREEN checkpoints cover failure-interrupted cycles, schema loading,
archive cleanup errors, parent-stream closure and incomplete replay evidence.
The stream-closure plus subagent checkpoint ran 53 tests. Each production fix was
validated against its failing target before broader regression testing.

Coverage of the new production modules (statement coverage from the engine suite):

| Module | Statements | Missed | Coverage |
|---|---:|---:|---:|
| `execution/react/progress.py` | 64 | 0 | 100% |
| `execution/react/tool_round.py` | 275 | 26 | 91% |
| `tool/output_store.py` | 58 | 1 | 98% |
| Total | 397 | 27 | 93% |

## Acceptance traceability

- O1–O4: `engine/tests/tool/test_output_store.py`, archive-collision regression in
  `engine/tests/execution/test_harness_regressions.py`.
- R1–R3: `test_tool_round_runner.py` and `test_tool_round_protocol.py`, plus existing
  `test_react_approval.py`, `test_react_budget.py`, `test_runtime_contract.py`,
  `test_reasoning_roundtrip.py` and `test_sub_agent.py` under `engine/tests/execution/`.
- P1–P3: `test_progress_ledger.py`, changing-poll regression in
  `test_harness_regressions.py`, and replay-evidence contract in `test_tool_round_runner.py`.

Existing failure limits, scoped tools, approvals, hook blocks, reasoning round-trip,
context fitting and execution-ledger behavior are covered by the full engine suite.
Seatbelt tests initially failed under a nested macOS sandbox (`sandbox_apply:
Operation not permitted`). The final engine run allowed the test process to create
its own Seatbelt sandbox; those integration cases passed with temporary fixtures.

## Known baseline issues

The server test
`test_token_stats_service.py::test_startup_backfill_never_locks_out_a_concurrent_request_write`
fails its timing ratio when run in the complete suite, both here and in a clean
archive of the base commit. The branch measured a 0.48s request wait during a
0.65s backfill; main measured 0.46s during 0.69s. The isolated retry passed.
No server source, timing assertion or test skip was changed. The five skips and
FastAPI/httpx deprecation warning are also pre-existing.

`pip-audit` against the existing server virtual environment reported 76 advisories
across seven installed packages:

| Package | Installed version | Advisories |
|---|---|---:|
| anyio | 4.14.1 | 3 |
| certifi | 2026.6.17 | 2 |
| cryptography | 49.0.0 | 2 |
| dnspython | 2.8.0 | 1 |
| pillow | 12.3.0 | 38 |
| pypdf | 6.14.2 | 27 |
| urllib3 | 2.7.0 | 3 |

This is an environment audit, not a claim that every advisory is reachable through
Smith. The PR introduces no dependencies or lockfile changes. Dependency upgrades
remain a separate maintenance task; this audit is not marked clean.

## Reproduction

From the repository root, with the project dependencies, pytest, pytest-asyncio and
pytest-cov installed in a Python 3.11+ environment:

```sh
PYTHONPATH="$PWD" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  -p pytest_asyncio.plugin -p pytest_cov.plugin -q engine/tests \
  --cov=engine.execution.react.progress \
  --cov=engine.execution.react.tool_round --cov=engine.tool.output_store \
  --cov-report=term-missing

PYTHONPATH="$PWD:$PWD/server" PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
  -p pytest_asyncio.plugin -q server/tests
```

Tests isolate AppPaths in temporary directories. No model credentials or live model
requests are needed. The original working checkout was not edited; this branch was
built in an independent clone and contains only the relevant protocol correction,
Harness implementation, tests and documentation.
