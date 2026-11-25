import type { ApprovalDetail, PendingApproval } from "./api.js";
import { sanitizeTerminalText } from "./sanitize.js";
import { truncateDisplay } from "./text-layout.js";

const DETAIL_ORDER = ["command", "cwd", "network", "path", "file_path", "timeout"];
const DETAIL_LABELS: Record<string, string> = {
  command: "Command",
  cwd: "Working directory",
  network: "Outbound network",
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
  // The key is a raw model-authored tool-argument name; sanitize it like the
  // values so escape sequences can never reach the terminal through a label.
  return sanitizeTerminalText(key)
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
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
  if (approval.tool === "shell") {
    if (approval.arguments.network === true || approval.scope?.kind === "sandbox_network") {
      return "Run a shell command with outbound network";
    }
    return "Run a shell command";
  }
  return `Use ${oneLine(approval.tool) || "a tool"}`;
}

export function approvalReason(approval: PendingApproval): string {
  return oneLine(approval.presentation?.reason || approval.reason);
}

export function approvalDetails(approval: PendingApproval): ApprovalDetail[] {
  const baseDetails: ApprovalDetail[] = approval.presentation?.details.length
    ? approval.presentation.details.map((detail) => ({
        label: oneLine(detail.label, 80),
        value: oneLine(detail.value),
      }))
    : (() => {
        const entries = Object.entries(approval.arguments).filter(
          ([, value]) => value !== undefined && value !== "",
        );
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
      })();

  const existingLabels = new Set(baseDetails.map((item) => item.label.toLowerCase()));
  const annotations: ApprovalDetail[] = [];

  if (approval.taintSources?.length && !existingLabels.has("taint warning")) {
    annotations.push({
      label: "Taint warning",
      value: oneLine(`Session read private data: ${approval.taintSources.join(", ")}`),
    });
  }
  if (approval.repeatCount !== undefined && approval.repeatCount >= 2 && !existingLabels.has("repeat guard")) {
    annotations.push({
      label: "Repeat guard",
      value: `Attempt #${approval.repeatCount} in this run`,
    });
  }
  if (approval.ruleId && !existingLabels.has("rule")) {
    annotations.push({
      label: "Rule",
      value: oneLine(approval.ruleId),
    });
  }

  return [...annotations, ...baseDetails];
}
