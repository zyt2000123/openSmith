"""Regression tests for the host execution backend's timeout handling.

A command that times out is killed and reaped; the result must still carry the
output that was read before the kill and the process's real exit status,
otherwise callers can neither show partial work nor distinguish a killed
command from one that never started.
"""

from __future__ import annotations

import asyncio
import sys

from engine.sandbox import LocalExecutionEnvironment


def test_timeout_preserves_partial_output_and_the_reaped_exit_code() -> None:
    result = asyncio.run(
        LocalExecutionEnvironment().run_command(
            argv=[
                sys.executable,
                "-c",
                (
                    "import sys, time; "
                    "sys.stdout.write('partial-output'); sys.stdout.flush(); "
                    "time.sleep(30)"
                ),
            ],
            timeout_seconds=0.3,
        )
    )

    assert result.timed_out
    assert result.exit_code is not None
    assert "partial-output" in result.stdout
