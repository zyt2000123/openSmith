"""Coding-domain quality gates: tests, validation, design evidence, git, PR.

Content layer: loaded by ``engine.execution.skill_chain.load_gate_content``.
Only coding pipelines should reference these keys; other domains ship their
own directory next to this one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GateResult:
    """Content-only gate decision; the engine adapts this duck-typed value."""

    verdict: str
    reason: str
    retry_hint: str | None = None


class TestGate:
    """Check that output has >= 2 test case descriptions and edge case coverage."""

    async def check(self, output: str, context: dict) -> GateResult:
        # Count distinct test mentions
        test_patterns = re.findall(
            r"测试用例|test\s*case|should\s|expect\s|assert|it\s*\(",
            output,
            re.IGNORECASE,
        )
        num_tests = len(test_patterns)

        has_edge = bool(re.search(
            r"边界|异常|edge|boundary|error|corner|negative",
            output,
            re.IGNORECASE,
        ))

        if num_tests >= 2 and has_edge:
            return GateResult("pass", f"Output includes {num_tests} test cases with edge case coverage.")

        missing = []
        if num_tests < 2:
            missing.append(f"at least 2 test case descriptions (found {num_tests})")
        if not has_edge:
            missing.append("edge case / boundary / error coverage")
        return GateResult(
            "retry",
            f"Testing output missing: {', '.join(missing)}.",
            retry_hint="Include at least 2 concrete test cases with expected behavior, and cover edge cases (boundary conditions, error paths).",
        )


class ValidationGate:
    """Check for evidence of actual execution and pass/fail results."""

    async def check(self, output: str, context: dict) -> GateResult:
        has_execution = bool(re.search(
            r"运行|执行|ran\b|executed|output|结果|stdout|stderr|\$\s",
            output,
            re.IGNORECASE,
        ))

        # Pass/fail with specifics — not just the bare word "passed"
        has_results = bool(re.search(
            r"(\d+\s*(passed|failed|tests?))|"
            r"(pass(?:ed)?.*\d)|(fail(?:ed)?.*\d)|"
            r"(✓|✗|PASS|FAIL)|"
            r"(通过\s*\d)|(失败\s*\d)|"
            r"(test.*result)|"
            r"(assert.*(?:true|false|equal))",
            output,
            re.IGNORECASE,
        ))

        if has_execution and has_results:
            return GateResult("pass", "Validation shows execution evidence and pass/fail results.")

        missing = []
        if not has_execution:
            missing.append("evidence of actual execution (e.g., command output, logs)")
        if not has_results:
            missing.append("specific pass/fail results (not just the word 'passed')")
        return GateResult(
            "retry",
            f"Validation output missing: {', '.join(missing)}.",
            retry_hint="Run the tests/commands and include the actual output showing pass/fail counts or specific results.",
        )


class ValidationLLMGate(ValidationGate):
    llm_prompt = (
        "Verify this validation report shows REAL evidence of execution (actual command outputs, "
        "test results, file changes). Not just claims of 'tests passed' without evidence.\n\n"
        "Validation output:\n{output}"
    )


class RootCauseGate:
    """Check for evidence-backed root cause analysis."""

    async def check(self, output: str, context: dict) -> GateResult:
        has_cause = bool(re.search(
            r"根因|root\s*cause|原因是|caused\s*by|根本原因",
            output,
            re.IGNORECASE,
        ))

        has_evidence = bool(re.search(
            r"证据|evidence|因为|because|日志|log|stack|trace|"
            r"堆栈|输出|显示|表明",
            output,
            re.IGNORECASE,
        ))

        if has_cause and has_evidence:
            return GateResult("pass", "Root cause with supporting evidence found.")

        missing = []
        if not has_cause:
            missing.append("root cause statement (e.g., '根因: ...' or 'Root cause: ...')")
        if not has_evidence:
            missing.append("supporting evidence (logs, stack traces, or behavioral observation)")
        return GateResult(
            "retry",
            f"Root cause analysis missing: {', '.join(missing)}.",
            retry_hint="State the root cause explicitly and cite evidence (logs, stack traces, output, or behavioral observation).",
        )


class SkillRubricGate:
    """Heuristic rubric for skill output quality (no LLM call).

    Evaluation criteria:
    1. Completeness — output length > 50 chars
    2. Evidence — contains at least one code block or file reference for technical skills
    3. No error markers — output does not contain failure indicators
    """

    # Patterns indicating the output itself reports a failure
    _ERROR_PATTERNS = re.compile(
        r"\[ERROR\]|(?<!\w)failed(?!\w)|unable to(?:\s)",
        re.IGNORECASE,
    )

    # Code block or file reference (path-like: foo/bar.py, src/main.ts, etc.)
    _CODE_OR_FILE = re.compile(
        r"```|[\w./-]+\.\w{1,5}",
    )

    async def check(self, output: str, context: dict) -> GateResult:
        stripped = output.strip()
        missing: list[str] = []

        # 1. Completeness — non-trivial length
        if len(stripped) <= 50:
            missing.append(f"output too short ({len(stripped)} chars, need >50)")

        # 2. Evidence — code block or file reference
        if not self._CODE_OR_FILE.search(stripped):
            missing.append("at least one code block or file reference")

        # 3. No error markers
        error_match = self._ERROR_PATTERNS.search(stripped)
        if error_match:
            missing.append(f"output contains error marker: '{error_match.group()}'")

        if not missing:
            return GateResult("pass", "Skill output meets rubric (completeness, evidence, no errors).")

        return GateResult(
            "retry",
            f"Rubric check failed: {'; '.join(missing)}.",
            retry_hint=(
                "Provide a more complete response (>50 chars) with concrete code blocks "
                "or file references, and resolve any errors before reporting results."
            ),
        )


class DesignGate:
    """Check that architecture/design output mentions affected files, data flow, and dependencies."""

    _AFFECTED_FILES = re.compile(
        r"affected\s+files?|涉及文件|修改文件|文件列表|files?\s+to\s+(change|modify|create)|"
        r"[\w./-]+\.\w{1,5}",  # or any concrete file path
        re.IGNORECASE,
    )

    _DATA_FLOW = re.compile(
        r"data\s*flow|数据流|流程|→|->|flow|sequence|调用链|请求流|pipeline",
        re.IGNORECASE,
    )

    _DEPENDENCIES = re.compile(
        r"dependenc|依赖|import|require|depend\s+on|引入|引用|上下游",
        re.IGNORECASE,
    )

    async def check(self, output: str, context: dict) -> GateResult:
        missing: list[str] = []

        if not self._AFFECTED_FILES.search(output):
            missing.append("affected files")
        if not self._DATA_FLOW.search(output):
            missing.append("data flow")
        if not self._DEPENDENCIES.search(output):
            missing.append("dependencies")

        if not missing:
            return GateResult("pass", "Design output covers affected files, data flow, and dependencies.")

        return GateResult(
            "fail",
            f"Design output missing: {', '.join(missing)}.",
            retry_hint=f"Include the following in your design: {', '.join(missing)}.",
        )


class GitWorktreeGate:
    """Check that git worktree is properly set up with no conflicts."""

    async def check(self, output: str, context: dict) -> GateResult:
        has_worktree = bool(re.search(
            r"worktree|工作树|work.?tree.*(created|ready|set.?up)|"
            r"切换到.*分支|checkout|switched\s+to",
            output,
            re.IGNORECASE,
        ))

        has_conflict = bool(re.search(
            r"conflict|冲突|CONFLICT|merge.?conflict|"
            r"unmerged|both\s+modified|"
            r"<<<<<<|>>>>>>|======",
            output,
            re.IGNORECASE,
        ))

        has_clean_state = bool(re.search(
            r"clean|干净|no\s+changes|nothing\s+to\s+commit|"
            r"working\s+tree\s+clean|ready|就绪",
            output,
            re.IGNORECASE,
        ))

        if has_conflict:
            return GateResult(
                "fail",
                "Git conflicts detected in worktree.",
                retry_hint="Resolve merge conflicts before proceeding. Run 'git status' to see conflicted files.",
            )

        if has_worktree and has_clean_state:
            return GateResult("pass", "Worktree is set up and in a clean state.")

        missing = []
        if not has_worktree:
            missing.append("evidence of worktree setup (e.g., 'worktree created at ...')")
        if not has_clean_state:
            missing.append("evidence of clean working state (e.g., 'working tree clean')")
        return GateResult(
            "retry",
            f"Worktree gate missing: {', '.join(missing)}.",
            retry_hint="Set up a git worktree and confirm it is in a clean state with no conflicts.",
        )


class PRGate:
    """Check commit messages follow conventions and no forbidden files are staged."""

    _FORBIDDEN_PATTERNS = re.compile(
        r"(?i)"
        r"\.env($|\.)|"
        r"credentials|"
        r"secrets?[./]|"
        r"\.pem$|\.key$|"
        r"id_rsa|id_ed25519|"
        r"\.aws/|\.ssh/"
    )

    _CONVENTIONAL_PREFIX = re.compile(
        r"(feat|fix|docs|style|refactor|test|chore|build|ci|perf|revert)"
        r"(\(.+\))?:\s",
        re.IGNORECASE,
    )

    async def check(self, output: str, context: dict) -> GateResult:
        forbidden_found: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            match = self._FORBIDDEN_PATTERNS.search(stripped)
            if match:
                if re.match(r"^[MADRCU?\s]{1,3}\s", stripped) or "staged" in stripped.lower():
                    forbidden_found.append(stripped)

        if forbidden_found:
            return GateResult(
                "fail",
                f"Forbidden files staged: {'; '.join(forbidden_found[:5])}.",
                retry_hint="Remove sensitive files from staging: git reset HEAD <file>. Add them to .gitignore.",
            )

        has_commit = bool(re.search(
            r"commit\s+[a-f0-9]|committed|提交|已提交",
            output,
            re.IGNORECASE,
        ))

        has_conventional = bool(self._CONVENTIONAL_PREFIX.search(output))

        has_message = bool(re.search(
            r"commit.*message|提交信息|提交消息|"
            r"-m\s+['\"]|"
            r"(feat|fix|docs|refactor|test|chore).*:",
            output,
            re.IGNORECASE,
        ))

        if has_commit and (has_conventional or has_message):
            return GateResult("pass", "Commit follows conventions with no forbidden files.")

        missing = []
        if not has_commit:
            missing.append("evidence of a commit (commit hash or confirmation)")
        if not has_conventional and not has_message:
            missing.append("conventional commit message (e.g., 'feat: ...', 'fix: ...')")
        return GateResult(
            "retry",
            f"PR gate missing: {', '.join(missing)}.",
            retry_hint="Use conventional commit format: 'feat(scope): description' or 'fix: description'. Ensure no .env, credentials, or key files are staged.",
        )


class TestDeliveryGate:
    """Check that tests were actually run and results appear in the output.

    NOTE: kept for parity with the pre-refactor engine; it had no registry
    key then and has none now. Give it a key here when a pipeline needs it.
    """

    async def check(self, output: str, context: dict) -> GateResult:
        has_test_run = bool(re.search(
            r"pytest|unittest|npm\s+test|yarn\s+test|"
            r"go\s+test|cargo\s+test|make\s+test|"
            r"运行.*测试|执行.*测试|test.*run|"
            r"running\s+tests|ran\s+\d+\s+test",
            output,
            re.IGNORECASE,
        ))

        has_test_counts = bool(re.search(
            r"\d+\s*(passed|failed|error|skipped|tests?\s+(passed|failed))|"
            r"(passed|failed|error)\s*:?\s*\d|"
            r"\d+\s*个.*(通过|失败|跳过)|"
            r"(通过|失败)\s*\d|"
            r"Tests:\s*\d|"
            r"OK\s*\(\d+\s*test|"
            r"FAILED\s*\(|"
            r"✓\s*\d|✗\s*\d|"
            r"\d+\s+passing|"
            r"\d+\s+failing",
            output,
            re.IGNORECASE,
        ))

        has_artifacts = bool(re.search(
            r"coverage|覆盖率|\d+%|"
            r"duration|耗时|\d+(\.\d+)?s\b|"
            r"test.*report|测试报告",
            output,
            re.IGNORECASE,
        ))

        if has_test_run and has_test_counts:
            return GateResult(
                "pass",
                "Tests were executed and results are present in output.",
            )

        if has_test_run and has_artifacts and not has_test_counts:
            return GateResult(
                "retry",
                "Tests appear to have run but specific pass/fail counts are missing.",
                retry_hint="Include the full test output showing how many tests passed/failed (e.g., '5 passed, 0 failed').",
            )

        missing = []
        if not has_test_run:
            missing.append("evidence of test execution (e.g., 'pytest ...', 'npm test')")
        if not has_test_counts:
            missing.append("test result counts (e.g., '5 passed, 0 failed')")
        return GateResult(
            "retry",
            f"Test delivery missing: {', '.join(missing)}.",
            retry_hint="Run the test suite and include the full output showing pass/fail counts. Do not just claim tests passed — show the actual output.",
        )


class GrillingCompleteGate:
    """Require actual shared-understanding evidence after grilling."""

    _FIELDS = {
        "scope": r"scope|范围",
        "non-goals": r"non-goals?|非目标",
        "constraints": r"constraints?|约束",
        "acceptance": r"acceptance(?:\s+signal)?|验收(?:标准|信号)?",
    }

    async def check(self, output: str, context: dict) -> GateResult:
        def has_field_value(field_pattern: str) -> bool:
            # Accept an inline label ("Scope: …") or a Markdown heading with
            # a non-empty following line. Bare mentions of a field do not
            # establish a user decision.
            return bool(re.search(
                rf"(?:{field_pattern})[ \t]*(?::|：)[ \t]*\S|"
                rf"(?:^|\n)[ \t#>*-]*(?:{field_pattern})[ \t]*\n[ \t]*\S",
                output,
                re.IGNORECASE,
            ))

        missing = [
            label for label, pattern in self._FIELDS.items()
            if not has_field_value(pattern)
        ]
        if not missing:
            return GateResult("pass", "User decisions are ready for research.")
        return GateResult(
            "retry",
            "Grilling is missing non-empty decision fields: " + ", ".join(missing) + ".",
            retry_hint=(
                "If a decision remains, ask exactly one question. Otherwise summarize "
                "scope, non-goals, constraints, and acceptance signal with a value for each."
            ),
        )


class ResearchBriefGate:
    """Require a real, local, source-attributed ResearchBrief artifact."""

    _PATH = re.compile(r"(?:^|[`\s(])(?P<path>docs/research/[A-Za-z0-9_./-]+\.md)(?:$|[`\s):,])")
    _HEADINGS = (
        "## problem",
        "## evidence",
        "## assumptions",
        "## open questions",
        "## recommendation",
    )

    async def check(self, output: str, context: dict) -> GateResult:
        match = self._PATH.search(output)
        working_dir = context.get("_working_dir")
        if match is None or not isinstance(working_dir, str) or not working_dir:
            return GateResult(
                "retry",
                "ResearchBrief must report a relative docs/research/*.md artifact path.",
            )
        workspace = Path(working_dir).resolve()
        research_root = (workspace / "docs" / "research").resolve()
        artifact = (workspace / match.group("path")).resolve()
        if not artifact.is_relative_to(research_root) or not artifact.is_file():
            return GateResult(
                "retry",
                "Reported ResearchBrief artifact does not exist under docs/research.",
                retry_hint="Create the cited Markdown file inside docs/research before reporting completion.",
            )
        text = artifact.read_text(encoding="utf-8").casefold()
        missing = [heading for heading in self._HEADINGS if heading not in text]
        if missing:
            return GateResult(
                "retry",
                f"ResearchBrief is missing required sections: {', '.join(missing)}.",
            )
        if "http" not in text:
            return GateResult(
                "retry",
                "ResearchBrief has no source citation link.",
                retry_hint="Cite the primary source URLs behind the findings.",
            )
        return GateResult("pass", f"ResearchBrief verified at {match.group('path')}.")


class PlanConfirmedGate:
    """Do not let a requirements plan advance before explicit confirmation."""

    _CONFIRMATION = re.compile(
        r"\b(?:yes|approved|confirm(?:ed)?|go ahead)\b|"
        r"(?:^|[\s，,。.!！；;])(?:我)?确认(?:[\s，,。.!！；;]|$)|"
        r"已确认|同意(?:[\s，,。.!！；;]|$)|批准|按此方案继续|可以开始|继续执行",
        re.IGNORECASE,
    )

    async def check(self, output: str, context: dict) -> GateResult:
        # Confirmation is a user decision, not an LLM-controlled output token.
        # A resumed paused node receives the answer in ``user_response``; a
        # single-turn request may include it in ``chain_request`` instead.
        user_evidence = "\n".join(
            str(context.get(key) or "")
            for key in ("user_response", "chain_request", "user_message")
        )
        if self._CONFIRMATION.search(user_evidence):
            return GateResult("pass", "Requirements plan has explicit user confirmation.")
        return GateResult(
            "retry",
            "Plan confirmation is missing.",
            retry_hint=(
                "Present the grounded plan, ask for an explicit user decision, and do not "
                "implement code until the user confirms it."
            ),
        )


class RedLoopGate:
    """A bug handoff must prove a runnable, symptom-specific RED loop."""

    _READY = "<!-- agent-smith:red-loop-ready -->"
    _COMMAND = re.compile(
        r"(?:^|\n)\s*(?:\$\s*)?(?:uv\s+run|pytest|python(?:3)?\s|npm\s+test|pnpm\s+test|yarn\s+test|"
        r"bun\s+test|go\s+test|cargo\s+test|curl\s|playwright\s)",
        re.IGNORECASE,
    )
    _RED_RESULT = re.compile(r"\b(?:failed|failure|error|failing|red)\b|失败|报错|异常", re.IGNORECASE)

    async def check(self, output: str, context: dict) -> GateResult:
        missing = []
        if self._READY not in output:
            missing.append("red-loop completion marker")
        if not self._COMMAND.search(output):
            missing.append("runnable reproduction command")
        if not self._RED_RESULT.search(output):
            missing.append("observed failing result")
        if not missing:
            return GateResult("pass", "Bug diagnosis produced a red-capable feedback loop.")
        return GateResult(
            "retry",
            f"Bug diagnosis missing: {', '.join(missing)}.",
            retry_hint=(
                "Run and report one deterministic command that exercises the user's exact symptom, "
                "including its observed failure, then end with <!-- agent-smith:red-loop-ready -->."
            ),
        )


class TddEvidenceGate:
    """Require both observed RED and observed GREEN in the implementation handoff."""

    _RED = re.compile(r"\bRED\b[\s\S]{0,500}\b(?:failed|failure|error|failing)\b|RED[\s\S]{0,500}(?:失败|报错|异常)", re.IGNORECASE)
    _GREEN = re.compile(r"\bGREEN\b[\s\S]{0,500}\b(?:passed|pass|success)\b|GREEN[\s\S]{0,500}(?:通过|成功)", re.IGNORECASE)
    _COMMAND = re.compile(r"(?:^|\n)\s*(?:\$\s*)?(?:uv\s+run|pytest|python(?:3)?\s|npm\s+test|pnpm\s+test|yarn\s+test|bun\s+test|go\s+test|cargo\s+test)", re.IGNORECASE)

    async def check(self, output: str, context: dict) -> GateResult:
        missing = []
        if not self._RED.search(output):
            missing.append("observed RED result")
        if not self._GREEN.search(output):
            missing.append("observed GREEN result")
        if not self._COMMAND.search(output):
            missing.append("test command evidence")
        if not missing:
            return GateResult("pass", "TddEvidence contains observed RED and GREEN evidence.")
        return GateResult(
            "retry",
            f"TDD implementation handoff missing: {', '.join(missing)}.",
            retry_hint="Report the actual RED and GREEN command outputs before declaring implementation ready.",
        )


class TddVerificationGate:
    """Require an evidence-backed final TddEvidence report."""

    _SECTIONS = ("build:", "types:", "lint:", "tests:", "security:", "diff:", "overall:")

    async def check(self, output: str, context: dict) -> GateResult:
        lowered = output.casefold()
        missing = []
        missing.extend(section for section in self._SECTIONS if section not in lowered)
        if missing:
            return GateResult(
                "retry",
                f"Verification handoff missing: {', '.join(missing)}.",
                retry_hint=(
                    "Return one consolidated TddEvidence with a VERIFICATION REPORT. Every phase must "
                    "be PASS, FAIL, or NOT_APPLICABLE with a reason, then end with "
                    "<!-- agent-smith:tdd-evidence-ready -->."
                ),
            )
        return GateResult("pass", "TddEvidence includes a complete verification report.")


class ReviewReportGate:
    """Keep Matt's Standards and Spec axes separate in the review artifact."""

    async def check(self, output: str, context: dict) -> GateResult:
        lowered = output.casefold()
        missing = []
        if "## standards" not in lowered:
            missing.append("Standards axis")
        if "## spec" not in lowered:
            missing.append("Spec axis")
        if not re.search(r"git\s+(?:diff|rev-parse|log)|fixed point|基线", output, re.IGNORECASE):
            missing.append("resolved fixed-point evidence")
        if missing:
            return GateResult(
                "retry",
                f"ReviewReport missing: {', '.join(missing)}.",
                retry_hint=(
                    "Resolve and report the fixed point, keep ## Standards and ## Spec separate, "
                    "then end with <!-- agent-smith:review-ready -->."
                ),
            )
        return GateResult("pass", "Two-axis ReviewReport is complete.")


class ReviewVerificationGate:
    """Require verification to be appended without collapsing review axes."""

    async def check(self, output: str, context: dict) -> GateResult:
        lowered = output.casefold()
        missing = []
        for section in ("## standards", "## spec", "## verification"):
            if section not in lowered:
                missing.append(section)
        if "overall:" not in lowered:
            missing.append("verification verdict")
        if missing:
            return GateResult(
                "retry",
                f"Verified ReviewReport missing: {', '.join(missing)}.",
                retry_hint=(
                    "Preserve the independent ## Standards and ## Spec sections, append ## Verification "
                    "with actual command results, and end with <!-- agent-smith:review-report-ready -->."
                ),
            )
        return GateResult("pass", "ReviewReport preserves both axes and verification evidence.")


GATES = {
    "test": TestGate,
    "validation_llm": ValidationLLMGate,
    "root_cause": RootCauseGate,
    "rubric": SkillRubricGate,
    "design": DesignGate,
    "git_worktree": GitWorktreeGate,
    "pr": PRGate,
    "grilling_complete": GrillingCompleteGate,
    "research_brief": ResearchBriefGate,
    "plan_confirmed": PlanConfirmedGate,
    "red_loop": RedLoopGate,
    "tdd_evidence": TddEvidenceGate,
    "tdd_verification": TddVerificationGate,
    "review_report": ReviewReportGate,
    "review_verification": ReviewVerificationGate,
}
