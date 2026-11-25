import assert from "node:assert/strict";
import test from "node:test";

import { approvalDetails, approvalReason, approvalSummary, approvalTitle, oneLine } from "./approval.js";

const shellApproval = {
  runId: "run-1",
  approvalId: "approval-1",
  tool: "shell",
  level: "execute",
  reason: "Approval required",
  arguments: {
    timeout: 60,
    cwd: "/workspace/shell",
    command: "npm test",
  },
};

test("approval summary describes the action before asking for a decision", () => {
  assert.equal(approvalSummary(shellApproval), "Smith wants to run this shell command:");
});

test("approval details put the command and execution context first", () => {
  assert.deepEqual(approvalDetails(shellApproval), [
    { label: "Command", value: "npm test" },
    { label: "Working directory", value: "/workspace/shell" },
    { label: "Timeout", value: "60" },
  ]);
});

test("approval details render unknown arguments and keep them on one line", () => {
  const details = approvalDetails({
    ...shellApproval,
    tool: "custom_tool",
    arguments: { payload: { ok: true }, note: "line 1\nline 2" },
  });

  assert.deepEqual(details, [
    { label: "Payload", value: '{"ok":true}' },
    { label: "Note", value: "line 1 line 2" },
  ]);
});

test("long approval values are truncated for the terminal prompt", () => {
  assert.equal(oneLine("x".repeat(10), 6), "xxxxx…");
});

test("approval detail text strips terminal control sequences", () => {
  const attack = `${String.fromCharCode(27)}]52;c;eA==${String.fromCharCode(7)}`;

  assert.equal(oneLine(`${attack}git status`), "git status");
  assert.deepEqual(approvalDetails({ ...shellApproval, arguments: { command: `${attack}git status` } }), [
    { label: "Command", value: "git status" },
  ]);
});

test("structured presentation wins over generic frontend tool-name fallbacks", () => {
  const approval = {
    ...shellApproval,
    tool: "git_ops",
    level: "write",
    presentation: {
      title: "Commit Git changes",
      summary: "Create a Git commit",
      details: [{ label: "Commit message", value: "fix approval" }],
      reason: "This changes repository history.",
    },
  };

  assert.equal(approvalTitle(approval), "Commit Git changes");
  assert.equal(approvalSummary(approval), "Create a Git commit");
  assert.deepEqual(approvalDetails(approval), [{ label: "Commit message", value: "fix approval" }]);
  assert.equal(approvalReason(approval), "This changes repository history.");
});

test("approval card annotates outbound network, taint warning, repeat guard, and rule", () => {
  const approval = {
    ...shellApproval,
    arguments: { command: "curl https://example.com", cwd: "/workspace", network: true },
    risk: "critical",
    ruleId: "ask-all-mcp-writes",
    taintSources: ["file:.env"],
    repeatCount: 2,
    scope: {
      kind: "sandbox_network",
      target: "curl https://example.com",
      access: ["workspace", "process", "network"],
      high_risk: true,
    },
  };

  assert.equal(approvalTitle(approval), "Run a shell command with outbound network");
  const details = approvalDetails(approval);
  assert.deepEqual(details.slice(0, 3), [
    { label: "Taint warning", value: "Session read private data: file:.env" },
    { label: "Repeat guard", value: "Attempt #2 in this run" },
    { label: "Rule", value: "ask-all-mcp-writes" },
  ]);
  assert.ok(details.some((d) => d.label === "Outbound network" && d.value === "true"));
});

test("degraded host_command with network=true labels NO OS SANDBOX instead of sandboxed", () => {
  const hostApproval = {
    ...shellApproval,
    arguments: { command: "curl https://example.com", network: true },
    scope: {
      kind: "host_command",
      target: "curl https://example.com",
      access: ["filesystem", "network", "process"],
      high_risk: true,
    },
  };

  assert.match(approvalTitle(hostApproval), /NO OS SANDBOX/);
  assert.match(approvalSummary(hostApproval), /NO OS SANDBOX/);
});

test("structured approval details stay single-line and bounded", () => {
  const details = approvalDetails({
    runId: "run-1",
    approvalId: "approval-1",
    tool: "shell",
    level: "execute",
    reason: "Approval required",
    arguments: {},
    presentation: {
      title: "Run command",
      summary: "Smith wants to run a command.",
      details: [{ label: "Command", value: "line one\nline two ".repeat(40) }],
      reason: "Need approval.",
    },
  });

  assert.equal(details[0]?.label, "Command");
  assert.equal(details[0]?.value.includes("\n"), false);
  assert.equal((details[0]?.value.length ?? 0) <= 240, true);
});
