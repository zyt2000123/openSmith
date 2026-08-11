import { localAuthHeaders } from "./auth.js";
import { sanitizeTerminalText, sanitizeUnknownText } from "./sanitize.js";
import { parseSmithUiPayload, type SmithUiPayload } from "./smith-ui-schema.js";

export const CONTEXT_DISPLAY_WINDOW = 128_000;

export type LlmUsage = "interactive" | "gate" | "background";
export type LlmTimeoutField = "connect" | "read" | "stream_read" | "write" | "pool";

export type LlmRoute = {
  provider?: string;
  base_url?: string;
  model?: string;
  stream?: boolean;
  max_output_tokens?: number;
  context_window?: number;
  timeout_profile?: LlmUsage;
  has_api_key: boolean;
};

export type LlmModelProfile = {
  provider?: string;
  base_url?: string;
  model?: string;
  stream?: boolean;
  max_output_tokens?: number;
  context_window?: number;
  has_api_key: boolean;
};

export type LlmTimeoutProfile = Partial<Record<LlmTimeoutField, number>>;

export type LlmConfig = {
  configured: boolean;
  has_api_key: boolean;
  /** Display-only name of the company or relay serving the model. */
  vendor?: string;
  provider: string;
  model: string;
  base_url: string;
  max_output_tokens: number | null;
  context_window?: number | null;
  routes: Partial<Record<LlmUsage, LlmRoute>>;
  models: Record<string, LlmModelProfile>;
  timeout_profiles: Partial<Record<LlmUsage, LlmTimeoutProfile>>;
};

export type RelayModelCatalog = {
  models: string[];
};

export type AgentProfile = {
  id: string;
  name: string;
  role: string;
  description?: string;
};

export type Session = {
  id: string;
  agent_id: string;
  title: string;
  created_at: string;
  last_message_preview?: string | null;
  last_message_at?: string | null;
  message_count: number;
  model_profile?: string | null;
};

export type SkillSummary = {
  name: string;
  description: string;
  source: string;
  version: string;
  argument_hint: string;
  /** Absent only when talking to a pre-enablements server; treat it as enabled. */
  enabled?: boolean;
};

export type TokenUsage = {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
};

export type TokenDay = TokenUsage & {
  date: string;
  sessions: number;
};

export type TokenModel = TokenUsage & {
  model: string;
  sessions: number;
};

export type TokenStats = TokenUsage & {
  year: number;
  session_count: number;
  active_days: number;
  current_streak: number;
  longest_streak: number;
  favorite_model: string | null;
  peak_hour: number | null;
  daily: TokenDay[];
  models: TokenModel[];
  estimated?: boolean;
};

export type ContextUsage = {
  context_tokens: number;
  context_window: number;
  context_percent: number;
  estimated: boolean;
  message_tokens?: number;
  tool_schema_tokens?: number;
  protocol_tokens?: number;
  effective_context_window?: number;
  safe_input_budget?: number;
  output_reserve?: number;
  safety_margin?: number;
  window_declared?: boolean;
  output_limit_declared?: boolean;
  fit_status?: string;
};

export type StreamTerminalStatus = "completed" | "failed" | "incomplete";

export type MaintenanceState = "idle" | "pending" | "running";

export type MemoryMaintenance = {
  compile: MaintenanceState;
  dream: MaintenanceState;
  /** Derived topic-knowledge lane; runs inside compile, so never "running". */
  topic_sync?: MaintenanceState;
  /**
   * Trailing run of failed automatic memory operations, with the newest
   * sanitized failure. A stalled pipeline (an expired provider key answering
   * 401 on every compile) must be visible in the HUD, not just in the API.
   * Optional so the shell keeps working against servers predating the field.
   */
  consecutive_failures?: number;
  last_error?: string | null;
};

export type RunState = {
  run_id: string;
  agent_id: string;
  session_id?: string | null;
  status: string;
  created_at: string;
  updated_at: string;
  event_seq: number;
  reason?: string | null;
  error?: string | null;
};

export type ApprovalDetail = {
  label: string;
  value: string;
};

export type ApprovalPresentation = {
  title: string;
  summary: string;
  details: ApprovalDetail[];
  reason: string;
};

export type PendingApproval = {
  runId: string;
  approvalId: string;
  tool: string;
  level: string;
  reason: string;
  arguments: Record<string, unknown>;
  presentation?: ApprovalPresentation;
};

export type StreamEvent =
  | { type: "message"; text: string }
  | { type: "smith_ui"; payload: SmithUiPayload }
  | { type: "smith_ui_fallback"; reason: string; code: string }
  | { type: "run_started"; runId: string }
  | ({ type: "approval_required" } & PendingApproval)
  | { type: "provisional_text_delta"; provisionId: string; text: string }
  | { type: "provisional_commit"; provisionId: string }
  | { type: "provisional_retract"; provisionId: string; reason: string }
  | { type: "thinking"; text: string; done: boolean }
  | { type: "tool_call"; id: string; name: string; hint: string }
  | { type: "tool_result"; id: string; error: boolean; blocked: boolean; preflight: boolean; summary: string }
  | { type: "skill"; name: string; status: string }
  | { type: "route_decided"; identityId: string; identityName: string; routeId: string; pipelineId: string }
  | { type: "gate_result"; skill: string; verdict: string; reason: string }
  | { type: "gate_evidence"; skill: string; evidenceHash: string; evidenceCount: number }
  | { type: "backtrack"; from: string; to: string; reason: string }
  | { type: "awaiting_input"; skill: string; reason: string }
  | ({ type: "token_usage" } & TokenUsage)
  | ({ type: "context_usage" } & ContextUsage)
  | { type: "compression"; active: boolean }
  | { type: "done"; id?: string; runId?: string; status: StreamTerminalStatus; reason?: string };

type RequestOptions = {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  timeoutMs?: number;
};

export const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;
export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 120_000;
// After the terminal SSE event, wait this long for a trailing usage frame in a
// later TCP read before giving up on the response closing.
const POST_DONE_DRAIN_MS = 30;
const MAX_SSE_FRAME_CHARS = 256 * 1024;

type TimeoutSignal = {
  signal: AbortSignal;
  didTimeout: () => boolean;
  touch: () => void;
  dispose: () => void;
};

export function createTimeoutSignal(timeoutMs: number, parentSignal?: AbortSignal): TimeoutSignal {
  const controller = new AbortController();
  let timedOut = false;

  const abortFromParent = () => {
    if (!controller.signal.aborted) {
      controller.abort(parentSignal?.reason ?? new DOMException("The request was aborted.", "AbortError"));
    }
  };

  const expire = () => {
    timedOut = true;
    controller.abort(new DOMException(`Request timed out after ${timeoutMs}ms.`, "TimeoutError"));
  };
  let timer = setTimeout(expire, timeoutMs);

  if (parentSignal?.aborted) {
    abortFromParent();
  } else {
    parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  }

  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    touch: () => {
      if (controller.signal.aborted) return;
      clearTimeout(timer);
      timer = setTimeout(expire, timeoutMs);
    },
    dispose: () => {
      clearTimeout(timer);
      parentSignal?.removeEventListener("abort", abortFromParent);
    },
  };
}

function timeoutError(timeoutMs: number): Error {
  return new Error(`Request timed out after ${timeoutMs}ms.`);
}

function finiteNumber(value: unknown, fallback: number): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

export type LlmRouteInput = {
  provider?: string | null;
  api_key?: string | null;
  base_url?: string | null;
  model?: string | null;
  stream?: boolean | null;
  max_output_tokens?: number | null;
  context_window?: number | null;
  timeout_profile?: LlmUsage | null;
};

export type LlmTimeoutProfileInput = Partial<Record<LlmTimeoutField, number | null>>;

export type LlmConfigInput = {
  vendor?: string;
  provider?: string;
  api_key?: string | null;
  base_url?: string;
  model?: string;
  max_output_tokens?: number | null;
  context_window?: number | null;
  routes?: Partial<Record<LlmUsage, LlmRouteInput | null>>;
  models?: Record<string, LlmRouteInput | null>;
  timeout_profiles?: Partial<Record<LlmUsage, LlmTimeoutProfileInput | null>>;
};

function buildUrl(baseUrl: string, pathname: string): string {
  // Resolve as a relative reference: `new URL("/api/x", "http://gw/smith/")`
  // discards `/smith`, so behind a sub-path reverse proxy the health probe
  // (which concatenates strings and keeps the prefix) passed while every real
  // request 404'd.
  return new URL(pathname.replace(/^\//, ""), `${baseUrl.replace(/\/$/, "")}/`).toString();
}

async function request<T>(baseUrl: string, pathname: string, options: RequestOptions = {}): Promise<T> {
  const authHeaders = await localAuthHeaders();
  const timeoutMs = options.timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS;
  const timeout = createTimeoutSignal(timeoutMs, options.signal);
  try {
    const response = await fetch(buildUrl(baseUrl, pathname), {
      method: options.method ?? "GET",
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        ...authHeaders,
      },
      signal: timeout.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });

    if (!response.ok) {
      const text = await response.text();
      throw new Error(`HTTP ${response.status}: ${terminalText(text || response.statusText)}`);
    }

    if (response.status === 204) return undefined as T;

    return (await response.json()) as T;
  } catch (error) {
    if (timeout.didTimeout()) throw timeoutError(timeoutMs);
    throw error;
  } finally {
    timeout.dispose();
  }
}

export async function getLlmConfig(baseUrl: string): Promise<LlmConfig> {
  return request<LlmConfig>(baseUrl, "/api/config/llm");
}

export async function listRelayModels(baseUrl: string): Promise<RelayModelCatalog> {
  return request<RelayModelCatalog>(baseUrl, "/api/config/llm/models");
}

export async function setLlmConfig(baseUrl: string, payload: LlmConfigInput): Promise<LlmConfig> {
  return request<LlmConfig>(baseUrl, "/api/config/llm", {
    method: "POST",
    body: payload,
  });
}

export async function ensureAgentProfile(baseUrl: string): Promise<AgentProfile> {
  return request<AgentProfile>(baseUrl, "/api/agent/ensure", {
    method: "POST",
  });
}

export type ProjectInstruction = {
  path: string;
  created: boolean;
};

export async function initializeProjectInstructions(baseUrl: string, workingDir: string): Promise<ProjectInstruction> {
  return request<ProjectInstruction>(baseUrl, "/api/agent/project-instructions", {
    method: "PUT",
    body: { working_dir: workingDir },
  });
}

export async function listSessions(baseUrl: string, options: Pick<RequestOptions, "signal"> = {}): Promise<Session[]> {
  const sessions = await request<Session[]>(baseUrl, "/api/agent/sessions", options);
  // Session titles come from the user/agent and are rendered raw in the sidebar;
  // sanitise at the same decode boundary as streamed content.
  return sessions.map((session) => ({ ...session, title: sanitizeTerminalText(session.title) }));
}

export async function getTokenStats(baseUrl: string, year?: number): Promise<TokenStats> {
  const query = year ? `?year=${encodeURIComponent(year)}` : "";
  return request<TokenStats>(baseUrl, `/api/agent/token-stats${query}`);
}

export type ObservabilityRun = {
  run_id: string;
  agent_id: string;
  session_id?: string | null;
  working_dir?: string | null;
  forced_skill?: string | null;
  created_at: string;
  finished_at: string;
  outcome?: string | null;
  reason?: string | null;
  event_count: number;
  tool_call_count: number;
  backtrack_count: number;
  approval_required_count: number;
  total_tokens: number;
};

export async function listObservabilityRuns(baseUrl: string, limit = 50): Promise<ObservabilityRun[]> {
  const runs = await request<ObservabilityRun[]>(baseUrl, `/api/agent/observability/runs?limit=${limit}`);
  // run.reason/outcome/forced_skill are model-authored and rendered in the run
  // explorer.
  return runs.map((run) => ({
    ...run,
    outcome: run.outcome ? sanitizeTerminalText(run.outcome) : null,
    reason: run.reason ? sanitizeTerminalText(run.reason) : null,
    forced_skill: run.forced_skill ? sanitizeTerminalText(run.forced_skill) : null,
  }));
}

export type RunDiagnosis = {
  run_id: string;
  agent_id: string;
  status: "healthy" | "needs_attention";
  failure_node?: string | null;
  primary_category?: string | null;
  summary: string;
  evidence: string[];
  recommendation?: string | null;
};

export async function getRunDiagnosis(baseUrl: string, runId: string): Promise<RunDiagnosis> {
  return request<RunDiagnosis>(baseUrl, `/api/agent/observability/runs/${encodeURIComponent(runId)}/diagnosis`);
}

export type AgentHealth = {
  agent_id: string;
  run_count: number;
  completed_count: number;
  unsuccessful_count: number;
  success_rate: number;
  tool_call_count: number;
  tool_success_rate?: number | null;
  average_backtracks: number;
  total_tokens: number;
  tokens_per_run: number;
};

export type RunIncident = {
  run_id: string;
  severity: "warning" | "error";
  category: string;
  message: string;
  occurred_at: string;
  evidence: Record<string, string | number>;
};

export type RunImprovementProposal = {
  status: "no_action" | "proposed";
  title: string;
  suggested_change?: string | null;
  approval_required: boolean;
};

export async function getObservabilityHealth(baseUrl: string): Promise<AgentHealth> {
  return request<AgentHealth>(baseUrl, "/api/agent/observability/health");
}

export async function listRunIncidents(baseUrl: string, limit = 20): Promise<RunIncident[]> {
  const incidents = await request<RunIncident[]>(baseUrl, `/api/agent/observability/incidents?limit=${limit}`);
  return incidents.map((incident) => ({
    ...incident,
    category: sanitizeTerminalText(incident.category),
    message: sanitizeTerminalText(incident.message),
  }));
}

export async function getRunImprovementProposal(baseUrl: string, runId: string): Promise<RunImprovementProposal> {
  return request<RunImprovementProposal>(
    baseUrl,
    `/api/agent/observability/runs/${encodeURIComponent(runId)}/improvement-proposal`,
  );
}

export async function createSession(
  baseUrl: string,
  title: string,
  modelProfile?: string | null,
  options: Pick<RequestOptions, "signal"> = {},
): Promise<Session> {
  return request<Session>(baseUrl, "/api/agent/sessions", {
    method: "POST",
    body: { title, ...(modelProfile ? { model_profile: modelProfile } : {}) },
    signal: options.signal,
  });
}

export type Message = {
  id: string;
  session_id: string;
  role: "user" | "assistant";
  content: string;
  created_at: string;
};

export async function listMessages(baseUrl: string, sessionId: string): Promise<Message[]> {
  const messages = await request<Message[]>(baseUrl, `/api/agent/sessions/${encodeURIComponent(sessionId)}/messages`);
  // Restored history is model-authored too, and it reaches the terminal through
  // the same renderers as live output — sanitise it on the same boundary.
  return messages.map((message) => ({ ...message, content: sanitizeTerminalText(message.content) }));
}

export async function listSkills(baseUrl: string): Promise<SkillSummary[]> {
  const skills = await request<SkillSummary[]>(baseUrl, "/api/agent/skills");
  // Skill descriptions are model/repo-authored (SKILL.md) and are rendered in
  // the /skills panels; sanitise at the decode boundary like streamed content.
  return skills.map((skill) => ({
    ...skill,
    name: sanitizeTerminalText(skill.name),
    description: sanitizeTerminalText(skill.description),
    source: sanitizeTerminalText(skill.source),
  }));
}

/**
 * Deferred memory maintenance state.
 *
 * Compilation, periodic candidate curation, and dreaming run as background
 * tasks that outlive the turn that scheduled them, so no per-run SSE stream
 * can carry their state — it is polled.
 */
export async function fetchMemoryMaintenance(baseUrl: string): Promise<MemoryMaintenance> {
  const maintenance = await request<MemoryMaintenance>(baseUrl, "/api/agent/memory/status");
  return {
    ...maintenance,
    last_error: maintenance.last_error ? sanitizeTerminalText(maintenance.last_error) : maintenance.last_error,
  };
}

export async function setSkillEnabled(baseUrl: string, skillName: string, enabled: boolean): Promise<SkillSummary> {
  return request<SkillSummary>(baseUrl, `/api/agent/skills/${encodeURIComponent(skillName)}`, {
    method: "PUT",
    body: { enabled },
  });
}

export type McpToolSummary = {
  name: string;
  description: string;
  inputSchema: Record<string, unknown>;
};

export type McpServer = {
  name: string;
  type: string;
  url?: string | null;
  command: string[];
  status: "connected" | "error";
  error?: string | null;
  tools: McpToolSummary[];
};

export async function listMcpServers(baseUrl: string): Promise<McpServer[]> {
  const servers = await request<McpServer[]>(baseUrl, "/api/agent/mcp");
  // MCP server/tool names and descriptions are config- or provider-authored and
  // rendered in the /mcp panel; sanitise at the decode boundary.
  return servers.map((server) => ({
    ...server,
    name: sanitizeTerminalText(server.name),
    error: server.error ? sanitizeTerminalText(server.error) : null,
    tools: server.tools.map((tool) => ({
      ...tool,
      name: sanitizeTerminalText(tool.name),
      description: sanitizeTerminalText(tool.description),
    })),
  }));
}

export type ContextCompression = {
  session_id: string;
  summary: string;
  message_count: number;
  context_summary_cutoff: number;
};

export async function compressSession(
  baseUrl: string,
  sessionId: string,
  signal?: AbortSignal,
): Promise<ContextCompression> {
  return request<ContextCompression>(baseUrl, `/api/agent/sessions/${encodeURIComponent(sessionId)}/compress`, {
    method: "POST",
    timeoutMs: 120_000,
    signal,
  });
}

export async function updateSessionModel(
  baseUrl: string,
  sessionId: string,
  modelProfile: string | null,
): Promise<Session> {
  return request<Session>(baseUrl, `/api/agent/sessions/${encodeURIComponent(sessionId)}/model`, {
    method: "PATCH",
    body: { model_profile: modelProfile },
  });
}

export async function resolveRunApproval(
  baseUrl: string,
  runId: string,
  approvalId: string,
  approved: boolean,
): Promise<void> {
  await request<void>(baseUrl, `/api/agent/runs/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    body: { approval_id: approvalId, approved },
  });
}

export async function getRun(baseUrl: string, runId: string): Promise<RunState> {
  return request<RunState>(baseUrl, `/api/agent/runs/${encodeURIComponent(runId)}`);
}

export async function deleteSession(baseUrl: string, sessionId: string): Promise<void> {
  await request<void>(baseUrl, `/api/agent/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}

type StreamOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
};

type StreamMessageOptions = StreamOptions & {
  context?: string;
  skillName?: string;
  workingDir?: string;
};

type ParsedSseChunk = {
  eventName: string;
  payload: Record<string, unknown>;
};

function splitSseBuffer(buffer: string): { chunks: string[]; remainder: string } {
  // Split on the SSE frame separator (a blank line). The SSE grammar permits
  // CRLF, bare LF, and bare CR line endings, including mixed pairs.
  const chunks: string[] = [];
  const boundary = /(?:\r\n|(?<!\r)\n|\r(?!\n))(?:\r\n|(?<!\r)\n|\r(?!\n))/g;
  let lastIndex = 0;
  let match = boundary.exec(buffer);

  while (match !== null) {
    chunks.push(buffer.slice(lastIndex, match.index));
    lastIndex = match.index + match[0].length;
    match = boundary.exec(buffer);
  }

  return { chunks, remainder: buffer.slice(lastIndex) };
}

function assertSseFrameLimit(chunks: string[], remainder: string): void {
  if (remainder.length > MAX_SSE_FRAME_CHARS || chunks.some((chunk) => chunk.length > MAX_SSE_FRAME_CHARS)) {
    throw new Error("SSE frame exceeded the 256 KiB limit.");
  }
}

function parseSseChunk(rawChunk: string): ParsedSseChunk | null {
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of rawChunk.replace(/\r\n?/g, "\n").split("\n")) {
    const separator = line.indexOf(":");
    if (separator < 1) continue;

    const field = line.slice(0, separator);
    const value = line.slice(separator + 1).replace(/^ /, "");
    if (field === "event") eventName = value;
    if (field === "data") dataLines.push(value);
  }

  if (dataLines.length === 0) return null;

  try {
    const payload = JSON.parse(dataLines.join("\n"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error(`Invalid payload in SSE ${eventName} event.`);
    }
    return { eventName, payload: payload as Record<string, unknown> };
  } catch {
    // Without a sample there is no way to learn what the server actually sent.
    // Sanitised and bounded: the payload is untrusted and reaches a terminal.
    throw new Error(
      `Invalid JSON in SSE ${eventName} event: ${sanitizeTerminalText(dataLines.join("\n")).slice(0, 200)}`,
    );
  }
}

type SseEventDecoder = (payload: Record<string, unknown>) => StreamEvent | null;

function terminalStatus(payload: Record<string, unknown>): StreamTerminalStatus {
  if (payload.status === "failed" || payload.status === "incomplete") return payload.status;
  return "completed";
}

function objectPayload(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

// FastAPI 422 bodies embed the failing input value verbatim, which can be a
// megabyte-sized message or a secret; cap display text so an error body can
// never flood the status line or the persisted transcript.
const TERMINAL_TEXT_MAX_LENGTH = 2_000;

/** Convert a decoded wire value into terminal-safe display text. */
function terminalText(value: unknown): string {
  const text = sanitizeTerminalText(typeof value === "string" ? value : String(value ?? ""));
  if (text.length <= TERMINAL_TEXT_MAX_LENGTH) return text;
  return text.slice(0, TERMINAL_TEXT_MAX_LENGTH) + "…";
}

function approvalPresentation(payload: Record<string, unknown>): ApprovalPresentation | undefined {
  const raw = objectPayload(payload.presentation);
  if (Object.keys(raw).length === 0) return undefined;

  const details = Array.isArray(raw.details)
    ? raw.details.flatMap((item) => {
        const detail = objectPayload(item);
        const label = terminalText(detail.label).trim();
        return label ? [{ label, value: terminalText(detail.value) }] : [];
      })
    : [];
  const title = terminalText(raw.title).trim();
  const summary = terminalText(raw.summary).trim();
  const reason = terminalText(raw.reason).trim();
  if (!title && !summary && details.length === 0 && !reason) return undefined;
  return {
    title: title || "Approval required",
    summary,
    details,
    reason,
  };
}

function smithUiFallback(payload: Record<string, unknown>, defaultReason: string): StreamEvent {
  const reason =
    typeof payload.reason === "string" && payload.reason ? terminalText(payload.reason).slice(0, 500) : defaultReason;
  const directCode = payload.code;
  if (typeof directCode === "string" && directCode.length <= 16_000) {
    return { type: "smith_ui_fallback", reason, code: terminalText(directCode) };
  }
  const code = terminalText(JSON.stringify(payload, null, 2));
  return { type: "smith_ui_fallback", reason, code: code.length <= 16_000 ? code : `${code.slice(0, 15_980)}\n…` };
}

const SSE_EVENT_DECODERS: Partial<Record<string, SseEventDecoder>> = {
  message: (payload) => ({ type: "message", text: sanitizeUnknownText(payload.text) }),
  smith_ui: (payload) => {
    const parsed = parseSmithUiPayload(payload);
    return parsed ? { type: "smith_ui", payload: parsed } : smithUiFallback(payload, "Unsupported smith-ui payload");
  },
  smith_ui_fallback: (payload) => smithUiFallback(payload, "Smith-ui validation failed"),
  run_started: (payload) => ({ type: "run_started", runId: String(payload.run_id ?? "") }),
  approval_required: (payload) => {
    const presentation = approvalPresentation(payload);
    return {
      type: "approval_required",
      runId: String(payload.run_id ?? ""),
      approvalId: String(payload.approval_id ?? ""),
      tool: String(payload.tool ?? "tool"),
      level: String(payload.level ?? "execute"),
      reason: sanitizeUnknownText(payload.reason).slice(0, 500) || "Approval required",
      arguments: objectPayload(payload.arguments),
      ...(presentation ? { presentation } : {}),
    };
  },
  provisional_text_delta: (payload) => ({
    type: "provisional_text_delta",
    provisionId: String(payload.provision_id ?? ""),
    text: sanitizeUnknownText(payload.text),
  }),
  provisional_commit: (payload) => ({
    type: "provisional_commit",
    provisionId: String(payload.provision_id ?? ""),
  }),
  provisional_retract: (payload) => ({
    type: "provisional_retract",
    provisionId: String(payload.provision_id ?? ""),
    reason: sanitizeUnknownText(payload.reason),
  }),
  thinking: (payload) => ({
    type: "thinking",
    text: sanitizeUnknownText(payload.text),
    done: Boolean(payload.done),
  }),
  tool_call: (payload) => ({
    type: "tool_call",
    id: String(payload.id ?? ""),
    name: sanitizeUnknownText(payload.name) || "tool",
    hint: sanitizeUnknownText(payload.hint),
  }),
  tool_result: (payload) => ({
    type: "tool_result",
    id: String(payload.id ?? ""),
    error: Boolean(payload.error),
    blocked: Boolean(payload.blocked),
    preflight: Boolean(payload.preflight),
    summary: sanitizeUnknownText(payload.summary),
  }),
  skill: (payload) => ({
    type: "skill",
    name: terminalText(payload.name),
    status: terminalText(payload.status),
  }),
  route_decided: (payload) => ({
    type: "route_decided",
    identityId: terminalText(payload.identity_id).slice(0, 120),
    identityName: terminalText(payload.identity_name).slice(0, 120),
    routeId: terminalText(payload.route_id).slice(0, 120),
    pipelineId: terminalText(payload.pipeline_id).slice(0, 120),
  }),
  gate_result: (payload) => ({
    type: "gate_result",
    skill: terminalText(payload.skill).slice(0, 120),
    verdict: terminalText(payload.verdict).slice(0, 80),
    reason: terminalText(payload.reason).slice(0, 500),
  }),
  gate_evidence: (payload) => ({
    type: "gate_evidence",
    skill: terminalText(payload.skill).slice(0, 120),
    evidenceHash: terminalText(payload.evidence_hash).slice(0, 128),
    evidenceCount: Math.max(0, Math.floor(finiteNumber(payload.evidence_count, 0))),
  }),
  backtrack: (payload) => ({
    type: "backtrack",
    from: terminalText(payload.from).slice(0, 120),
    to: terminalText(payload.to).slice(0, 120),
    reason: terminalText(payload.reason).slice(0, 500),
  }),
  awaiting_input: (payload) => ({
    type: "awaiting_input",
    skill: terminalText(payload.skill).slice(0, 120),
    reason: terminalText(payload.reason).slice(0, 500),
  }),
  token_usage: (payload) => ({
    type: "token_usage",
    input_tokens: finiteNumber(payload.input_tokens, 0),
    output_tokens: finiteNumber(payload.output_tokens, 0),
    total_tokens: finiteNumber(payload.total_tokens, 0),
  }),
  context_usage: (payload) => ({
    type: "context_usage",
    context_tokens: finiteNumber(payload.context_tokens, 0),
    context_window: finiteNumber(payload.context_window, CONTEXT_DISPLAY_WINDOW),
    context_percent: finiteNumber(payload.context_percent, 0),
    estimated: Boolean(payload.estimated ?? true),
    message_tokens: finiteNumber(payload.message_tokens, 0),
    tool_schema_tokens: finiteNumber(payload.tool_schema_tokens, 0),
    protocol_tokens: finiteNumber(payload.protocol_tokens, 0),
    effective_context_window: finiteNumber(
      payload.effective_context_window ?? payload.context_window,
      CONTEXT_DISPLAY_WINDOW,
    ),
    safe_input_budget: finiteNumber(payload.safe_input_budget ?? payload.context_window, CONTEXT_DISPLAY_WINDOW),
    output_reserve: finiteNumber(payload.output_reserve, 0),
    safety_margin: finiteNumber(payload.safety_margin, 0),
    window_declared: Boolean(payload.window_declared ?? false),
    output_limit_declared: Boolean(payload.output_limit_declared ?? false),
    fit_status: String(payload.fit_status ?? "unknown"),
  }),
  compression: (payload) => ({
    type: "compression",
    active: Boolean(payload.active),
  }),
  done: (payload) => ({
    type: "done",
    id: payload.id ? String(payload.id) : undefined,
    ...(payload.run_id ? { runId: String(payload.run_id) } : {}),
    ...(typeof payload.reason === "string" && payload.reason ? { reason: payload.reason.slice(0, 500) } : {}),
    status: terminalStatus(payload),
  }),
};

export function decodeSseEvent(rawChunk: string): StreamEvent | null {
  const parsed = parseSseChunk(rawChunk);
  if (!parsed) return null;

  const { eventName, payload } = parsed;
  if (eventName === "error") throw new Error(terminalText(payload.message ?? payload.error) || "Server stream failed.");

  const decoder = Object.hasOwn(SSE_EVENT_DECODERS, eventName) ? SSE_EVENT_DECODERS[eventName] : undefined;
  return decoder ? decoder(payload) : null;
}

function consumeSseChunks(chunks: string[], sawDone: boolean): { events: StreamEvent[]; sawDone: boolean } {
  const events: StreamEvent[] = [];
  let completed = sawDone;

  for (const chunk of chunks) {
    const event = decodeSseEvent(chunk);
    if (!event) continue;
    if (completed) {
      // After the terminal event only usage counters are legitimate: a server
      // may legally frame token_usage/context_usage after done (even in a later
      // TCP read), and dropping them would permanently undercount the session
      // totals.  Stale content events after done are dropped.
      if (event.type === "token_usage" || event.type === "context_usage") {
        events.push(event);
      }
      continue;
    }
    events.push(event);
    if (event.type === "done") completed = true;
  }

  return { events, sawDone: completed };
}

async function* readSseEvents(
  body: ReadableStream<Uint8Array>,
  onActivity?: () => void,
): AsyncGenerator<StreamEvent, void, void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawDone = false;

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      onActivity?.();

      buffer += decoder.decode(value, { stream: true });
      const parsed = splitSseBuffer(buffer);
      assertSseFrameLimit(parsed.chunks, parsed.remainder);
      buffer = parsed.remainder;
      const consumed = consumeSseChunks(parsed.chunks, sawDone);
      sawDone = consumed.sawDone;
      yield* consumed.events;
      if (sawDone) {
        // The server usually closes right after done, but a TCP packet split can
        // put a trailing token_usage/context_usage frame in later reads.  Drain
        // within a short budget, then stop — never hang the turn waiting for the
        // response to close.
        const deadline = Date.now() + POST_DONE_DRAIN_MS;
        for (;;) {
          const remaining = deadline - Date.now();
          if (remaining <= 0) return;
          const trailing = await Promise.race([
            reader.read(),
            new Promise<"timeout">((resolve) => {
              const timer = setTimeout(() => resolve("timeout"), remaining);
              timer.unref?.();
            }),
          ]);
          if (trailing === "timeout") return;
          const { done: trailingDone, value: trailingValue } = trailing;
          if (trailingDone) break;
          if (!trailingValue || trailingValue.length === 0) return;
          buffer += decoder.decode(trailingValue, { stream: true });
          const trailingParsed = splitSseBuffer(buffer);
          assertSseFrameLimit(trailingParsed.chunks, trailingParsed.remainder);
          // Consume the buffer, exactly as the main loop does.  Leaving the parsed
          // frames behind re-yielded them on the next drain read and again in the
          // post-loop flush, and after `done` the only events still allowed
          // through are the usage counters — which the store *accumulates*, so a
          // trailing token_usage arriving in its own TCP read doubled the turn and
          // session totals.
          buffer = trailingParsed.remainder;
          const trailingConsumed = consumeSseChunks(trailingParsed.chunks, sawDone);
          yield* trailingConsumed.events;
        }
      }
    }

    buffer += decoder.decode();
    const parsed = splitSseBuffer(buffer);
    assertSseFrameLimit(parsed.chunks, parsed.remainder);
    const chunks = parsed.remainder.trim() ? [...parsed.chunks, parsed.remainder] : parsed.chunks;
    const consumed = consumeSseChunks(chunks, sawDone);
    yield* consumed.events;
    if (!consumed.sawDone) {
      throw new Error("SSE stream ended before a done event was received.");
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // The response may already be closed or aborted.
    }
    reader.releaseLock();
  }
}

async function* streamRequest(
  baseUrl: string,
  pathname: string,
  body: unknown,
  options: StreamOptions = {},
): AsyncGenerator<StreamEvent, void, void> {
  const authHeaders = await localAuthHeaders();
  const timeoutMs = options.timeoutMs ?? DEFAULT_STREAM_IDLE_TIMEOUT_MS;
  const timeout = createTimeoutSignal(timeoutMs, options.signal);
  try {
    const response = await fetch(buildUrl(baseUrl, pathname), {
      method: "POST",
      headers: {
        Accept: "text/event-stream",
        "Content-Type": "application/json",
        ...authHeaders,
      },
      signal: timeout.signal,
      body: body === undefined ? undefined : JSON.stringify(body),
    });

    if (!response.ok) {
      const text = await response.text();
      // Same treatment as request(): terminal escape sequences must not reach
      // the renderer, and a 422 body embeds the failing input (which can be a
      // large message or a secret), so sanitize and cap it.
      throw new Error(`HTTP ${response.status}: ${terminalText(text || response.statusText)}`);
    }

    if (!response.body) {
      throw new Error("Streaming response body is missing.");
    }

    yield* readSseEvents(response.body, timeout.touch);
  } catch (error) {
    if (timeout.didTimeout()) throw timeoutError(timeoutMs);
    throw error;
  } finally {
    timeout.dispose();
  }
}

export async function* streamMessage(
  baseUrl: string,
  sessionId: string,
  content: string,
  options: StreamMessageOptions = {},
): AsyncGenerator<StreamEvent, void, void> {
  yield* streamRequest(
    baseUrl,
    `/api/agent/sessions/${encodeURIComponent(sessionId)}/messages/stream`,
    {
      content,
      context: options.context,
      skill_name: options.skillName,
      working_dir: options.workingDir,
    },
    options,
  );
}

export async function* streamRunResume(
  baseUrl: string,
  runId: string,
  options: StreamOptions = {},
): AsyncGenerator<StreamEvent, void, void> {
  yield* streamRequest(baseUrl, `/api/agent/runs/${encodeURIComponent(runId)}/resume`, undefined, options);
}
