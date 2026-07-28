import type { ApprovalDetail, PendingApproval } from "./api.js";
import { sanitizeTerminalText } from "./sanitize.js";
import { truncateDisplay } from "./text-layout.js";

const DETAIL_ORDER = ["command", "cwd", "path", "file_path", "timeout"];
const DETAIL_LABELS: Record<string, string> = {
  command: "Command",
  cwd: "Working directory",
  path: "Path",
  file_path: "File",
  timeout: "Timeout",
};

export function oneLine(value: unknown, maxLength = 240): string {
  let text: string;
  if (typeof value === "string") {
    text = value;
  } else if (value === null) {
    text = "null";
  } else if (value === undefined) {
    text = "";
  } else if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    text = String(value);
  } else {
    try {
      text = JSON.stringify(value) ?? String(value);
    } catch {
      text = String(value);
    }
  }

  const compact = sanitizeTerminalText(text).replace(/\s+/g, " ").trim();
  return truncateDisplay(compact, maxLength);
}

function detailLabel(key: string): string {
  if (DETAIL_LABELS[key]) return DETAIL_LABELS[key];
  return key.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

export function approvalSummary(approval: PendingApproval): string {
  if (approval.presentation?.summary) return approval.presentation.summary;
  if (approval.tool === "shell") {
    return approval.arguments.command
      ? "Smith wants to run this shell command:"
      : "Smith wants to execute a shell command:";
  }
  return `Smith wants to use ${oneLine(approval.tool) || "this tool"} (${oneLine(approval.level) || "execute"}):`;
}

export function approvalTitle(approval: PendingApproval): string {
  if (approval.presentation?.title) return approval.presentation.title;
  return approval.tool === "shell" ? "Run a shell command" : `Use ${oneLine(approval.tool) || "a tool"}`;
}

export function approvalReason(approval: PendingApproval): string {
  return oneLine(approval.presentation?.reason || approval.reason);
}

export function approvalDetails(approval: PendingApproval): ApprovalDetail[] {
  if (approval.presentation?.details.length) {
    return approval.presentation.details.map((detail) => ({
      label: oneLine(detail.label, 80),
      value: oneLine(detail.value),
    }));
  }

  const entries = Object.entries(approval.arguments).filter(([, value]) => value !== undefined && value !== "");
  const ordered = [...entries].sort(([left], [right]) => {
    const leftIndex = DETAIL_ORDER.indexOf(left);
    const rightIndex = DETAIL_ORDER.indexOf(right);
    if (leftIndex === -1 && rightIndex === -1) return 0;
    if (leftIndex === -1) return 1;
    if (rightIndex === -1) return -1;
    return leftIndex - rightIndex;
  });

  return ordered.map(([key, value]) => ({
    label: detailLabel(key),
    value: oneLine(value),
  }));
}
