/**
 * NodeBridge — all backend communication goes through here.
 * UI components never call api.ts directly; they call bridge methods.
 */

import type { StoreApi } from "zustand/vanilla";

import { createToolActivity } from "./activity.js";
import {
  CONTEXT_DISPLAY_WINDOW,
  compressSession,
  createSession,
  deleteSession,
  ensureAgentProfile,
  getLlmConfig,
  getObservabilityHealth,
  getRun,
  getRunDiagnosis,
  getRunImprovementProposal,
  getTokenStats,
  initializeProjectInstructions,
  type LlmConfigInput,
  listMcpServers,
  listMessages,
  listObservabilityRuns,
  listRelayModels,
  listRunIncidents,
  listSessions,
  listSkills,
  type RunDiagnosis,
  type RunImprovementProposal,
  type RunState,
  resolveRunApproval,
  type Session,
  type StreamEvent,
  type StreamTerminalStatus,
  setLlmConfig,
  setSkillEnabled,
  streamMessage,
  streamRunResume,
  updateSessionModel,
} from "./api.js";
import { ensureLocalServer } from "./dev-server.js";
import { createModelPicker, type ModelPickerTarget } from "./model-picker.js";
import { MAX_QUEUED_MESSAGES, type QueuedMessage } from "./queue.js";
import { createSetupDraft } from "./setup.js";
import { type AppStore, TRANSCRIPT_LIMIT } from "./store.js";
import { clearTerminal } from "./term.js";
import { limitTranscript, removeApprovalNotice, restartLatestTurn, restoreTranscript } from "./transcript-state.js";

/** Shown wherever a missing baseUrl blocks the action — always with the way back out. */
const NOT_READY_STATUS = "Shell is not ready yet. Run /reconnect to retry the local server.";

export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatTraceStatus(diagnosis: RunDiagnosis, proposal: RunImprovementProposal | null): string {
  const heading = `Trace · ${diagnosis.run_id.slice(0, 10)} · ${diagnosis.status === "healthy" ? "healthy" : "attention"}`;
  const details = [
    `Summary: ${diagnosis.summary}`,
    diagnosis.failure_node ? `Node: ${diagnosis.failure_node}` : null,
    diagnosis.primary_category ? `Category: ${diagnosis.primary_category}` : null,
    diagnosis.evidence.length ? `Evidence: ${diagnosis.evidence.join(", ")}` : null,
    diagnosis.recommendation ? `Next: ${diagnosis.recommendation}` : null,
    proposal?.status === "proposed" ? `Proposal: ${proposal.title} (approval required)` : null,
    proposal?.suggested_change ? `Change: ${proposal.suggested_change}` : null,
    proposal === null ? "Proposal: unavailable; RCA remains available." : null,
  ].filter((line): line is string => Boolean(line));
  return [heading, ...details].join("\n");
}

// 逐 token 流式下每个 text_delta 触发一次 Ink 全量重绘；按 40ms 合帧。
function createTextBatcher(emit: (text: string) => void) {
  let pending = "";
  let timer: ReturnType<typeof setTimeout> | null = null;

  const flush = () => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (!pending) return;
    const batched = pending;
    pending = "";
    emit(batched);
  };

  return {
    push(text: string): void {
      pending += text;
      if (!timer) timer = setTimeout(flush, 40);
    },
    flush,
    discard(): void {
      if (timer) clearTimeout(timer);
      timer = null;
      pending = "";
    },
  };
}

function createProvisionalBatcher(emit: (provisionId: string, text: string) => void) {
  const pending = new Map<string, string>();
  let timer: ReturnType<typeof setTimeout> | null = null;

  const flush = (provisionId?: string) => {
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }

    if (provisionId !== undefined) {
      const text = pending.get(provisionId);
      if (!text) return;
      pending.delete(provisionId);
      emit(provisionId, text);
      return;
    }

    for (const [id, text] of pending) {
      emit(id, text);
    }
    pending.clear();
  };

  return {
    push(provisionId: string, text: string): void {
      if (!provisionId || !text) return;
      pending.set(provisionId, `${pending.get(provisionId) ?? ""}${text}`);
      if (!timer) timer = setTimeout(flush, 40);
    },
    flush,
    discard(): void {
      if (timer) clearTimeout(timer);
      timer = null;
      pending.clear();
    },
  };
}

type TextBatcher = ReturnType<typeof createTextBatcher>;
type ProvisionalBatcher = ReturnType<typeof createProvisionalBatcher>;
type TerminalOutcome = { status: StreamTerminalStatus; reason?: string };

export function terminalStatusMessage(status: StreamTerminalStatus, reason?: string): string {
  if (status === "incomplete" && reason === "model_output_limit") {
    return "Model output limit reached; the answer may be incomplete.";
  }
  if (reason === "blocked") {
    return "Agent was blocked; see the transcript for the reason.";
  }
  if (status === "incomplete") {
    return "Agent did not complete the task; see the transcript for the reason.";
  }
  return "Agent execution failed; see the transcript and server log for details.";
}

function applyBatchedStreamEvent(
  event: StreamEvent,
  messageBatcher: TextBatcher,
  provisionalBatcher: ProvisionalBatcher,
  applyEvent: (event: StreamEvent) => void,
): StreamTerminalStatus | null {
  if (event.type === "message") {
    provisionalBatcher.flush();
    messageBatcher.push(event.text);
    return null;
  }

  if (event.type === "provisional_text_delta") {
    messageBatcher.flush();
    provisionalBatcher.push(event.provisionId, event.text);
    return null;
  }

  messageBatcher.flush();
  if (event.type === "provisional_commit" || event.type === "provisional_retract") {
    provisionalBatcher.flush(event.provisionId);
  } else {
    provisionalBatcher.flush();
  }
  applyEvent(event);
  return event.type === "done" ? event.status : null;
}

export class NodeBridge {
  private activeRequest: AbortController | null = null;
  private tokenStatsRequestId = 0;
  private observabilityRequestId = 0;
  private booting = false;

  constructor(private store: StoreApi<AppStore>) {}

  private get s() {
    return this.store.getState();
  }

  enqueueMessage(text: string, skillName?: string): boolean {
    const normalized = text.trim();
    if (!normalized) return false;

    const queuedMessages = this.s.queuedMessages;
    if (queuedMessages.length >= MAX_QUEUED_MESSAGES) {
      this.s.set({ statusLine: `Queue is full (${MAX_QUEUED_MESSAGES}). Press Esc to remove the newest message.` });
      return false;
    }

    const item: QueuedMessage = {
      id: `queue-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      text: normalized,
      ...(skillName ? { skillName } : {}),
    };
    this.s.set({
      queuedMessages: [...queuedMessages, item],
      statusLine: `Queued ${queuedMessages.length + 1}/${MAX_QUEUED_MESSAGES}.`,
    });
    return true;
  }

  removeQueuedMessage(index: number): QueuedMessage | null {
    const queuedMessages = this.s.queuedMessages;
    const removed = queuedMessages[index];
    if (!removed) return null;

    this.s.set({ queuedMessages: queuedMessages.filter((_item, itemIndex) => itemIndex !== index) });
    return removed;
  }

  removeLatestQueuedMessage(): QueuedMessage | null {
    return this.removeQueuedMessage(this.s.queuedMessages.length - 1);
  }

  cancelRequest(): boolean {
    const controller = this.activeRequest;
    if (!controller || controller.signal.aborted) return false;

    controller.abort();
    this.s.interruptTurn("cancelled");
    const pendingApproval = this.s.pendingApproval;
    this.s.set({
      pendingApproval: null,
      approvalResolving: false,
      busy: false,
      runStartedAt: null,
      statusLine: "Cancelled.",
      ...(pendingApproval ? { transcript: removeApprovalNotice(this.s.transcript, pendingApproval.approvalId) } : {}),
    });
    return true;
  }

  async resolveApproval(approved: boolean): Promise<void> {
    const pending = this.s.pendingApproval;
    if (!pending) {
      this.s.set({ statusLine: "No approval is waiting." });
      return;
    }
    if (this.s.approvalResolving) return;
    const approvalId = pending.approvalId;
    const baseUrl = this.s.baseUrl;

    this.s.set({
      approvalResolving: true,
      statusLine: approved ? "Allowing the requested action…" : "Denying the requested action…",
    });
    try {
      await resolveRunApproval(baseUrl, pending.runId, approvalId, approved);
      if (this.s.pendingApproval?.approvalId !== approvalId) return;
      this.s.set({
        pendingApproval: null,
        transcript: removeApprovalNotice(this.s.transcript, pending.approvalId),
        statusLine: approved ? "Approval granted. Continuing…" : "Approval denied. Continuing safely…",
      });
    } catch (error) {
      if (this.s.pendingApproval?.approvalId === approvalId) {
        this.s.set({ statusLine: `Approval update failed: ${errorMessage(error)}` });
      }
    } finally {
      if (this.s.pendingApproval?.approvalId === approvalId) this.s.set({ approvalResolving: false });
    }
  }

  async boot(): Promise<void> {
    // /reconnect can be pressed repeatedly; without this each press would spawn
    // another uvicorn, and only the last one stays tracked for cleanup. Say so, or
    // the caller's "Reconnecting…" would sit there for a boot it never started.
    if (this.booting) {
      this.s.set({ statusLine: "Already reconnecting; waiting for the local server." });
      return;
    }
    this.booting = true;
    try {
      const server = await ensureLocalServer();
      this.s.set({
        baseUrl: server.baseUrl,
        statusLine: server.note ?? (server.started ? "Local server started." : "Connected to local server."),
      });

      const config = await getLlmConfig(server.baseUrl);
      this.s.set({ config });
      if (!config.configured) {
        const setupDraft = createSetupDraft(config);
        this.s.set({
          mode: "setup",
          setupFlow: "initial",
          setupDraft,
          setupIndex: 0,
          inputValue: setupDraft.provider,
          statusLine: "Run the initial setup to wake Smith up.",
        });
        return;
      }

      await this.hydrateShell(server.baseUrl);
    } catch (error) {
      const message = errorMessage(error);
      this.s.set({
        mode: "chat",
        panel: "welcome",
        statusLine: `Boot failed: ${message}`,
        // Without a baseUrl every other command dead-ends, so name the way back out.
        welcomeNotice: { text: `${message}\nFix the problem, then run /reconnect to retry.`, tone: "error" },
      });
    } finally {
      this.booting = false;
    }
  }

  async hydrateShell(baseUrl: string, bootNotes: string[] = []): Promise<void> {
    const config = this.s.config;
    if (!config) throw new Error("LLM configuration is unavailable.");

    const agent = await ensureAgentProfile(baseUrl);
    const warnings: string[] = [];
    const [sessions, skills] = await Promise.all([
      listSessions(baseUrl).catch((error: unknown) => {
        warnings.push(`Sessions unavailable: ${errorMessage(error)}`);
        return [];
      }),
      listSkills(baseUrl).catch((error: unknown) => {
        warnings.push(`Skills unavailable: ${errorMessage(error)}`);
        return [];
      }),
    ]);

    this.s.hydrate({
      agent,
      sessions,
      skills,
      mcpServers: this.s.mcpServers,
      config,
      notices: [...bootNotes, ...warnings],
    });
  }

  async saveConfig(input: LlmConfigInput): Promise<void> {
    const { baseUrl } = this.s;
    this.s.set({ busy: true, inputLocked: true, statusLine: "Saving configuration…" });
    try {
      const saved = await setLlmConfig(baseUrl, input);
      this.s.set({ config: saved });
      if (!saved.configured) {
        this.s.set({
          mode: "setup",
          setupIndex: 0,
          statusLine: "Interactive LLM route still needs an API key, base URL, and model.",
        });
        return;
      }
      await this.hydrateShell(baseUrl);
    } catch (error) {
      this.s.set({ statusLine: `Save failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ busy: false, inputLocked: false });
    }
  }

  async refreshSkills(): Promise<void> {
    const { baseUrl, agent } = this.s;
    if (baseUrl && agent) this.s.set({ skills: await listSkills(baseUrl) });
  }

  async setSkillEnabled(skillName: string, enabled: boolean): Promise<void> {
    const { baseUrl } = this.s;
    if (!baseUrl) return;

    this.s.set({ inputLocked: true, statusLine: `${enabled ? "Enabling" : "Disabling"} ${skillName}…` });
    try {
      const updated = await setSkillEnabled(baseUrl, skillName, enabled);
      const state = this.s;
      const isEnabled = updated.enabled !== false;
      state.set({
        skills: state.skills.map((skill) => (skill.name === updated.name ? updated : skill)),
        pendingSkill: !isEnabled && state.pendingSkill?.name === updated.name ? null : state.pendingSkill,
        statusLine: `${isEnabled ? "Enabled" : "Disabled"}: ${updated.name}`,
      });
    } catch (error) {
      this.s.set({ statusLine: `Skill update failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ inputLocked: false });
    }
  }

  async refreshMcpServers(): Promise<void> {
    const { baseUrl, agent } = this.s;
    if (!baseUrl || !agent) return;
    if (this.s.inputLocked) return;
    this.s.set({ inputLocked: true, statusLine: "Loading MCP servers…" });
    try {
      this.s.set({ mcpServers: await listMcpServers(baseUrl) });
    } catch (error) {
      this.s.pushSystemLine(`MCP unavailable: ${errorMessage(error)}`, "error");
    } finally {
      this.s.set({ inputLocked: false });
    }
  }

  async initializeProject(workingDir: string) {
    const { baseUrl } = this.s;
    if (!baseUrl) throw new Error("Shell is not ready yet.");
    return initializeProjectInstructions(baseUrl, workingDir);
  }

  async openTokenStats(): Promise<void> {
    const { baseUrl } = this.s;
    if (!baseUrl) {
      // Land on chat rather than the caller's panel: Tab cycles through here, and
      // staying put would make the key look dead. Name the way out, or the panel
      // just vanishes with no hint why.
      this.s.set({ panel: "chat", statusLine: NOT_READY_STATUS });
      return;
    }
    const requestId = ++this.tokenStatsRequestId;
    this.s.set({ panel: "tokens", tokenStats: null, statusLine: "Loading token statistics…" });
    try {
      const tokenStats = await getTokenStats(baseUrl);
      if (requestId !== this.tokenStatsRequestId || this.s.panel !== "tokens") return;
      this.s.set({ tokenStats, panel: "tokens", statusLine: "Token statistics ready." });
    } catch (error) {
      if (requestId !== this.tokenStatsRequestId || this.s.panel !== "tokens") return;
      this.s.pushSystemLine(`Token statistics unavailable: ${errorMessage(error)}`, "error");
      this.s.set({ panel: "chat", statusLine: "Token statistics unavailable." });
    }
  }

  async openRunExplorer(): Promise<void> {
    const { baseUrl } = this.s;
    if (!baseUrl) {
      this.s.set({ panel: "chat", statusLine: NOT_READY_STATUS });
      return;
    }
    const requestId = ++this.observabilityRequestId;
    this.s.set({
      panel: "runs",
      observabilityRuns: null,
      observabilityHealth: null,
      observabilityIncidents: null,
      statusLine: "Loading observability…",
    });
    try {
      const [runsResult, healthResult, incidentsResult] = await Promise.allSettled([
        listObservabilityRuns(baseUrl),
        getObservabilityHealth(baseUrl),
        listRunIncidents(baseUrl),
      ]);
      if (runsResult.status === "rejected") throw runsResult.reason;
      if (requestId !== this.observabilityRequestId || this.s.panel !== "runs") return;
      const observabilityRuns = runsResult.value;
      const observabilityHealth = healthResult.status === "fulfilled" ? healthResult.value : null;
      const observabilityIncidents = incidentsResult.status === "fulfilled" ? incidentsResult.value : null;
      const unavailable = [
        healthResult.status === "rejected" ? "health" : null,
        incidentsResult.status === "rejected" ? "incidents" : null,
      ].filter((value): value is string => value !== null);
      this.s.set({
        panel: "runs",
        observabilityRuns,
        observabilityHealth,
        observabilityIncidents,
        statusLine: `${observabilityRuns.length} recorded run(s) · ${observabilityIncidents?.length ?? 0} incident(s).${
          unavailable.length ? ` ${unavailable.join(" and ")} unavailable.` : ""
        }`,
      });
    } catch (error) {
      if (requestId !== this.observabilityRequestId || this.s.panel !== "runs") return;
      this.s.pushSystemLine(`Run history unavailable: ${errorMessage(error)}`, "error");
      this.s.set({ panel: "chat", statusLine: "Run history unavailable." });
    }
  }

  async showTrace(requestedRunId?: string): Promise<void> {
    const { baseUrl, currentSession } = this.s;
    if (!baseUrl) {
      this.s.set({ statusLine: NOT_READY_STATUS });
      return;
    }
    this.s.set({ statusLine: "Loading run trace…" });
    try {
      const runs = requestedRunId ? [] : await listObservabilityRuns(baseUrl, 20);
      const runId =
        requestedRunId ?? runs.find((run) => run.session_id === currentSession?.id)?.run_id ?? runs[0]?.run_id;
      if (!runId) {
        this.s.set({ statusLine: "No recorded runs yet." });
        return;
      }
      const [diagnosisResult, proposalResult] = await Promise.allSettled([
        getRunDiagnosis(baseUrl, runId),
        getRunImprovementProposal(baseUrl, runId),
      ]);
      if (diagnosisResult.status === "rejected") throw diagnosisResult.reason;
      const diagnosis = diagnosisResult.value;
      const proposal = proposalResult.status === "fulfilled" ? proposalResult.value : null;
      this.s.pushSystemLine(formatTraceStatus(diagnosis, proposal));
      this.s.set({ panel: "chat", statusLine: `Trace for ${runId.slice(0, 10)}.` });
    } catch (error) {
      this.s.pushSystemLine(`Trace unavailable: ${errorMessage(error)}`, "error");
      this.s.set({ statusLine: "Trace unavailable." });
    }
  }

  async selectModel(modelProfile: string | null): Promise<void> {
    const { baseUrl, config, currentSession } = this.s;
    if (modelProfile !== null && !config?.models?.[modelProfile]) {
      this.s.set({ statusLine: `Unknown model profile: ${modelProfile}` });
      return;
    }
    if (this.s.inputLocked || this.s.compressing) return;
    if (!currentSession) {
      this.s.set({
        selectedModelProfile: modelProfile,
        statusLine: modelProfile ? `Model selected: ${modelProfile}.` : "Default model selected.",
      });
      return;
    }

    this.s.set({ inputLocked: true, statusLine: "Updating the session model…" });
    try {
      const session = await updateSessionModel(baseUrl, currentSession.id, modelProfile);
      this.s.set({
        selectedModelProfile: modelProfile,
        ...(session ? { currentSession: session } : {}),
        statusLine: modelProfile ? `Model selected: ${modelProfile}.` : "Default model selected.",
      });
    } catch (error) {
      this.s.set({ statusLine: `Model selection failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ inputLocked: false });
    }
  }

  async openModelPicker(): Promise<void> {
    const { baseUrl, config } = this.s;
    if (!config?.configured) {
      this.s.set({ statusLine: "Configure the default relay before loading models." });
      return;
    }
    if (this.s.inputLocked || this.s.compressing) return;

    this.s.set({ busy: true, inputLocked: true, statusLine: "Loading models from the relay…" });
    try {
      const catalog = await listRelayModels(baseUrl);
      if (catalog.models.length === 0) {
        this.s.set({ statusLine: "The relay returned no models. Use /model add <model-id> [profile]." });
        return;
      }
      this.s.set({
        modelPicker: createModelPicker(catalog.models),
        inputValue: "",
        statusLine: `${catalog.models.length} relay model(s). Choose one and press Enter.`,
      });
    } catch (error) {
      this.s.set({ statusLine: `Model discovery failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ busy: false, inputLocked: false });
    }
  }

  async applyDiscoveredModel(model: string, target: ModelPickerTarget): Promise<void> {
    const { baseUrl, config } = this.s;
    const modelId = model.trim();
    if (!config?.configured || !modelId) {
      this.s.set({ statusLine: "The selected model is unavailable." });
      return;
    }
    if (this.s.inputLocked || this.s.compressing) return;

    this.s.set({ busy: true, inputLocked: true, statusLine: "Updating model configuration…" });
    try {
      const saved = await setLlmConfig(baseUrl, {
        provider: config.provider,
        base_url: config.base_url,
        model: target === "primary" ? modelId : config.model,
        ...(target === "review" ? { routes: { gate: { model: modelId } } } : {}),
      });
      this.s.set({
        config: saved,
        modelPicker: null,
        statusLine:
          target === "primary" ? `Primary model updated to ${modelId}.` : `Review model updated to ${modelId}.`,
      });
    } catch (error) {
      this.s.set({ statusLine: `Model configuration failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ busy: false, inputLocked: false });
    }
  }

  async addModelProfile(model: string, profileName = model): Promise<void> {
    const { baseUrl, config } = this.s;
    const modelId = model.trim();
    const name = profileName.trim();
    if (!config?.configured) {
      this.s.set({ statusLine: "Configure the default relay before adding models." });
      return;
    }
    if (!modelId || !name) {
      this.s.set({ statusLine: "Model ID and profile name are required." });
      return;
    }
    if (name === "default" || name === "base") {
      this.s.set({ statusLine: `Profile name ${name} is reserved.` });
      return;
    }

    this.s.set({ busy: true, inputLocked: true, statusLine: `Adding model profile ${name}…` });
    try {
      const saved = await setLlmConfig(baseUrl, {
        provider: config.provider,
        base_url: config.base_url,
        model: config.model,
        models: { [name]: { model: modelId } },
      });
      const action = config.models[name] ? "Updated" : "Added";
      this.s.set({
        config: saved,
        statusLine: `${action} ${name} (${modelId}). It reuses the default relay; switch with /model ${name}.`,
      });
    } catch (error) {
      this.s.set({ statusLine: `Model add failed: ${errorMessage(error)}` });
    } finally {
      this.s.set({ busy: false, inputLocked: false });
    }
  }

  async compressCurrentSession(): Promise<void> {
    const { baseUrl, currentSession, busy } = this.s;
    if (busy) {
      this.s.set({ statusLine: "Wait for the current task to finish before compressing." });
      return;
    }
    if (!currentSession) {
      this.s.set({ statusLine: "No active session to compress." });
      return;
    }
    this.s.set({ busy: true, compressing: true, panel: "chat", statusLine: "Automatically compressing context" });
    try {
      const result = await compressSession(baseUrl, currentSession.id);
      this.s.pushSystemLine(`Context compressed: ${result.message_count} messages summarized.`);
      this.s.set({ statusLine: "Context compressed. The next request will use the saved summary." });
    } catch (error) {
      this.s.pushSystemLine(`Context compression failed: ${errorMessage(error)}`, "error");
      this.s.set({ statusLine: "Context compression failed." });
    } finally {
      this.s.set({ busy: false, compressing: false });
    }
  }

  startNewSession(): boolean {
    if ((this.activeRequest && !this.activeRequest.signal.aborted) || this.s.compressing || this.s.inputLocked) {
      this.s.set({ statusLine: "Cancel the current task before starting a new session." });
      return false;
    }

    clearTerminal();
    this.s.startFreshSession();
    return true;
  }

  reloadContext(): boolean {
    // PromptAssembler reads context files for each new run. A fresh session makes
    // the reload boundary explicit while preserving the old session in history.
    return this.startNewSession();
  }

  async clearCurrentSession(): Promise<boolean> {
    if ((this.activeRequest && !this.activeRequest.signal.aborted) || this.s.compressing || this.s.inputLocked) {
      this.s.set({ statusLine: "Cancel the current task before clearing the session." });
      return false;
    }

    const { baseUrl, currentSession } = this.s;
    if (!currentSession) {
      clearTerminal();
      this.s.startFreshSession();
      return true;
    }

    this.s.set({ inputLocked: true, statusLine: "Clearing current session…" });
    try {
      await deleteSession(baseUrl, currentSession.id);
      this.s.set({ sessions: this.s.sessions.filter((session) => session.id !== currentSession.id) });
    } catch (error) {
      this.s.pushSystemLine(`Session deletion failed: ${errorMessage(error)}`, "error");
      this.s.set({ statusLine: "Session was not cleared because deletion failed." });
      return false;
    } finally {
      this.s.set({ inputLocked: false });
    }

    clearTerminal();
    this.s.startFreshSession();
    return true;
  }

  async resumeSession(session: Session): Promise<void> {
    if ((this.activeRequest && !this.activeRequest.signal.aborted) || this.s.compressing || this.s.inputLocked) {
      this.s.set({ statusLine: "Finish the current shell operation before resuming a session." });
      return;
    }
    await this.loadSession(session);
  }

  private async loadSession(session: Session): Promise<boolean> {
    const { baseUrl } = this.s;
    this.s.set({ inputLocked: true, statusLine: `Loading ${session.id}…` });
    try {
      const messages = await listMessages(baseUrl, session.id);
      const transcript = limitTranscript(restoreTranscript(messages), TRANSCRIPT_LIMIT);
      clearTerminal();
      this.s.set({
        currentSession: session,
        selectedModelProfile: session.model_profile ?? null,
        transcript,
        transcriptEpoch: this.s.transcriptEpoch + 1,
        turnCount: transcript.filter((entry) => entry.kind === "turn").length,
        toolActivity: createToolActivity(),
        turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        tokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
        contextUsage: {
          context_tokens: 0,
          context_window: CONTEXT_DISPLAY_WINDOW,
          context_percent: 0,
          estimated: true,
        },
        queuedMessages: [],
        panel: "chat",
        statusLine: `Resumed ${session.id} (${messages.length} messages).`,
      });
      return true;
    } catch (error) {
      const message = errorMessage(error);
      if (/HTTP 404\b/.test(message)) {
        this.s.set({ sessions: this.s.sessions.filter((item) => item.id !== session.id) });
      }
      this.s.pushSystemLine(`Could not resume ${session.id}: ${message}`, "error");
      this.s.set({ statusLine: "Resume failed. The current session was kept." });
      return false;
    } finally {
      this.s.set({ inputLocked: false });
    }
  }

  async resumeRun(requestedRunId?: string): Promise<void> {
    if ((this.activeRequest && !this.activeRequest.signal.aborted) || this.s.compressing || this.s.inputLocked) {
      this.s.set({ statusLine: "Finish the current shell operation before recovering a run." });
      return;
    }

    const runId = requestedRunId ?? this.s.recoverableRunId;
    if (!runId) {
      this.s.set({ statusLine: "No recoverable run is known. Use /resume run <run-id>." });
      return;
    }

    const session = await this.prepareRunRecovery(runId);
    if (!session) return;

    const controller = this.startRequest();
    try {
      await this.streamResumedRun(this.s.baseUrl, runId, controller.signal);
      if (controller.signal.aborted) return;
      this.s.closeTurn();
      await this.refreshSessions(this.s.baseUrl, session, false, controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) this.reportRequestError(error);
    } finally {
      this.finishRequest(controller);
    }
  }

  private async prepareRunRecovery(runId: string): Promise<Session | null> {
    this.s.set({ inputLocked: true, statusLine: `Checking run ${runId}…` });
    try {
      const run = await getRun(this.s.baseUrl, runId);
      if (!this.isResumableRun(run)) return null;

      const session = await this.sessionForRun(run, runId);
      if (!session) return null;
      if (this.s.currentSession?.id !== session.id && !(await this.loadSession(session))) return null;
      return session;
    } catch (error) {
      this.s.pushSystemLine(`Could not recover ${runId}: ${errorMessage(error)}`, "error");
      this.s.set({ statusLine: "Run recovery failed. The current session was kept." });
      return null;
    } finally {
      this.s.set({ inputLocked: false });
    }
  }

  private isResumableRun(run: RunState): boolean {
    if (!run.session_id) {
      this.s.set({ statusLine: `Run ${run.run_id} is missing its session and cannot be recovered.` });
      return false;
    }
    if (new Set(["cancelled", "failed", "incomplete"]).has(run.status)) return true;

    this.s.set({ statusLine: `Run ${run.run_id} cannot be recovered from ${run.status}.` });
    return false;
  }

  private async sessionForRun(run: RunState, runId: string): Promise<Session | null> {
    const sessionId = run.session_id;
    if (!sessionId) return null;

    let session = this.s.sessions.find((candidate) => candidate.id === sessionId);
    if (!session) {
      const sessions = await listSessions(this.s.baseUrl);
      this.s.set({ sessions });
      session = sessions.find((candidate) => candidate.id === sessionId);
    }
    if (session) return session;

    this.s.set({ statusLine: `Run ${runId} refers to a session that is no longer available.` });
    return null;
  }

  async sendMessage(text: string, skillName?: string): Promise<boolean> {
    if (this.s.inputLocked || this.s.compressing) {
      this.s.set({ statusLine: "Wait for the current shell operation to finish." });
      return false;
    }
    if (this.activeRequest) return this.enqueueMessage(text, skillName);

    const ready = this.getReadySession();
    if (!ready) return false;

    const controller = this.startRequest();
    // 回显先于建会话的网络往返：失败时该轮也留在转录里，由 reportRequestError 收尾。
    this.s.pushTurn(text);
    try {
      const session = await this.getOrCreateSession(ready.baseUrl, ready.currentSession, text, controller.signal);
      if (controller.signal.aborted) return true;
      await this.streamResponse(ready.baseUrl, session, text, skillName, controller.signal);
      if (controller.signal.aborted) return true;
      this.s.closeTurn();
      await this.refreshSessions(ready.baseUrl, session, ready.currentSession === null, controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) this.reportRequestError(error);
    } finally {
      this.finishRequest(controller);
    }
    return true;
  }

  private getReadySession(): { baseUrl: string; currentSession: Session | null } | null {
    const { baseUrl, agent, currentSession } = this.s;
    if (baseUrl && agent) return { baseUrl, currentSession };

    // The only signal a user gets when a queued message cannot be handed off, so it
    // has to name /reconnect: the message stays queued until something drives it.
    this.s.set({ statusLine: NOT_READY_STATUS });
    return null;
  }

  private startRequest(): AbortController {
    const controller = new AbortController();
    this.activeRequest = controller;
    this.s.set({ busy: true, compressing: false, runStartedAt: Date.now(), panel: "chat", statusLine: "Processing…" });
    return controller;
  }

  private finishRequest(controller: AbortController): void {
    if (this.activeRequest !== controller) return;

    this.activeRequest = null;
    const next = this.takeQueuedMessage();
    if (next) {
      this.s.set({ pendingApproval: null, approvalResolving: false });
      void this.sendMessage(next.text, next.skillName).then((accepted) => {
        if (accepted) return;
        // The handoff was refused (a shell operation locked the input as the run ended).
        // Requeue and go idle, otherwise the spinner would run forever with no request
        // behind it. Push it back at the head, not through enqueueMessage: it already
        // holds a queue slot, so re-entering the tail would reorder it behind whatever
        // the user typed while the handoff was in flight — and if that filled the queue,
        // the cap would drop it outright. Overshooting the cap by one beats losing it.
        this.s.set({ queuedMessages: [next, ...this.s.queuedMessages] });
        if (!this.activeRequest) this.goIdle();
      });
      return;
    }
    this.goIdle();
  }

  private goIdle(): void {
    this.s.set({
      pendingApproval: null,
      approvalResolving: false,
      busy: false,
      compressing: false,
      runStartedAt: null,
    });
  }

  private takeQueuedMessage(): QueuedMessage | null {
    const next = this.s.queuedMessages[0];
    if (!next) return null;

    this.s.set({ queuedMessages: this.s.queuedMessages.slice(1) });
    return next;
  }

  private async getOrCreateSession(
    baseUrl: string,
    currentSession: Session | null,
    text: string,
    signal: AbortSignal,
  ): Promise<Session> {
    if (currentSession) return currentSession;

    const title = text.trim().split(/\n+/)[0]?.slice(0, 40) || "Smith Session";
    const session = await createSession(baseUrl, title, this.s.selectedModelProfile, { signal });
    if (signal.aborted) {
      try {
        await deleteSession(baseUrl, session.id);
      } catch {
        // Best-effort cleanup prevents a cancelled first message from leaving an orphan session.
      }
      throw signal.reason ?? new DOMException("The request was aborted.", "AbortError");
    }
    this.s.set({ currentSession: session });
    return session;
  }

  private async streamResponse(
    baseUrl: string,
    session: Session,
    text: string,
    skillName: string | undefined,
    signal: AbortSignal,
  ): Promise<void> {
    const terminalStatus = await this.consumeStream(
      streamMessage(baseUrl, session.id, text, {
        skillName,
        workingDir: process.env.SMITH_PROJECT_CWD?.trim() || process.cwd(),
        signal,
      }),
      signal,
    );
    this.reportTerminalStatus(terminalStatus, signal);
  }

  private async streamResumedRun(baseUrl: string, runId: string, signal: AbortSignal): Promise<void> {
    const terminalStatus = await this.consumeStream(
      this.resetTranscriptWhenResumeStarts(streamRunResume(baseUrl, runId, { signal }), runId),
      signal,
    );
    this.reportTerminalStatus(terminalStatus, signal);
  }

  private async *resetTranscriptWhenResumeStarts(
    events: AsyncIterable<StreamEvent>,
    runId: string,
  ): AsyncGenerator<StreamEvent, void, void> {
    let reset = false;
    for await (const event of events) {
      if (!reset && event.type === "run_started") {
        // Resuming into the current session skips loadSession, so this is the only
        // place that rewinds the turn. Replayed events re-add every tool call and
        // token, so the counters have to start from zero alongside the transcript.
        clearTerminal();
        this.s.set({
          transcript: restartLatestTurn(this.s.transcript),
          transcriptEpoch: this.s.transcriptEpoch + 1,
          toolActivity: createToolActivity(),
          turnTokenUsage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
          recoverableRunId: runId,
        });
        reset = true;
      }
      yield event;
    }
  }

  private async consumeStream(
    events: AsyncIterable<StreamEvent>,
    signal: AbortSignal,
  ): Promise<TerminalOutcome | null> {
    let terminalStatus: StreamTerminalStatus | null = null;
    let terminalReason: string | undefined;
    const messageBatcher = createTextBatcher((batched) => this.s.applyEvent({ type: "message", text: batched }));
    const provisionalBatcher = createProvisionalBatcher((provisionId, batched) =>
      this.s.applyEvent({ type: "provisional_text_delta", provisionId, text: batched }),
    );

    try {
      for await (const event of events) {
        if (signal.aborted) break;
        const status = applyBatchedStreamEvent(event, messageBatcher, provisionalBatcher, (next) =>
          this.s.applyEvent(next),
        );
        if (status) {
          terminalStatus = status;
          terminalReason = event.type === "done" ? event.reason : undefined;
        }
      }
    } finally {
      // 取消后该轮已被 closeTurn 定格，迟到的文本不再写入。
      if (signal.aborted) {
        messageBatcher.discard();
        provisionalBatcher.discard();
      } else {
        messageBatcher.flush();
        provisionalBatcher.flush();
      }
    }

    if (signal.aborted) return null;

    return terminalStatus ? { status: terminalStatus, reason: terminalReason } : null;
  }

  private reportTerminalStatus(terminal: TerminalOutcome | null, signal: AbortSignal): void {
    if (signal.aborted) return;
    if (!terminal) throw new Error("SSE stream ended before completion.");
    if (terminal.status === "completed") {
      this.s.set({ statusLine: "Ready. Type the next task or /help." });
      return;
    }

    const message = terminalStatusMessage(terminal.status, terminal.reason);
    this.s.pushSystemLine(`[warning] ${message}`, "error");
    this.s.set({ statusLine: message });
  }

  private async refreshSessions(
    baseUrl: string,
    session: Session,
    selectSession: boolean,
    signal: AbortSignal,
  ): Promise<void> {
    try {
      const sessions = await listSessions(baseUrl, { signal });
      if (signal.aborted) return;
      this.s.set({ sessions });
      if (!selectSession) return;

      const currentSession = sessions.find((item) => item.id === session.id);
      if (currentSession) this.s.set({ currentSession });
    } catch (error) {
      if (signal.aborted) return;
      this.s.pushSystemLine(`Session list unavailable: ${errorMessage(error)}`);
    }
  }

  private reportRequestError(error: unknown): void {
    const message = errorMessage(error);
    const pendingApproval = this.s.pendingApproval;
    this.s.interruptTurn("error");
    this.s.set({
      pendingApproval: null,
      approvalResolving: false,
      ...(pendingApproval ? { transcript: removeApprovalNotice(this.s.transcript, pendingApproval.approvalId) } : {}),
    });
    this.s.pushSystemLine(`[error] ${message}`, "error");
    this.s.set({ statusLine: "Request failed. See the transcript for details." });
  }
}
