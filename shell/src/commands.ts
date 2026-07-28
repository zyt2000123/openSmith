import type { SkillSummary } from "./api.js";
import { errorMessage, type NodeBridge } from "./bridge.js";
import { LIFECYCLE_HOOKS } from "./hooks.js";
import { createSetupDraft, fieldValue, setupFieldAt } from "./setup.js";
import { isSkillEnabled } from "./skill-mention.js";
import type { AppStore, Panel } from "./store.js";

export type SlashItem = {
  id: string;
  title: string;
  command: string;
  description: string;
  category: string;
};

type CommandContext = {
  bridge: NodeBridge;
  exit: () => void;
  getState: () => AppStore;
  workingDir?: string;
};

type CommandHandler = (args: string[], context: CommandContext) => Promise<void> | void;

const HELP_TEXT = [
  "- `/help` — show this list",
  "- `/new` — start a fresh session and keep the current session in history",
  "- `/reload` — start fresh after changing SMITH.md or other context files",
  "- `/init` — create a project .smith/SMITH.md instruction template",
  "- `/clear` — delete the current session and start fresh",
  "- `/compress` — summarize and persist the active session context",
  "- `/model` — discover relay models and configure the primary or review model",
  "- `/config [advanced]` — edit essential or routed LLM config",
  "- `/sessions` — recent sessions",
  "- `/token` — local token usage dashboard",
  "- `/runs` — recent Agent runs and outcome metrics",
  "- `/trace [run-id]` — diagnose the latest run or a specific Run",
  "- `/skills` — inspect or run a standard SKILL.md skill",
  "- `/skill <name> [prompt]` — arm a skill, or run it straight away with a prompt",
  "- `/hooks` — inspect runtime lifecycle hooks",
  "- `/mcp` — inspect configured MCP servers and tools",
  "- `/resume [session-id]` — recover the latest interrupted run, or open a session; use `/resume run <run-id>` for a specific run",
  "- `/compact` — switch to compact view; Ctrl+O toggles compact/transcript",
  "- `/reconnect` — retry the local server connection after a failed boot",
  "- `/exit` — quit",
].join("\n");

/** Skills are reached through `@name` and `/skill`, never through this palette. */
export function buildSlashItems(): SlashItem[] {
  const commands: SlashItem[] = [
    {
      id: "help",
      title: "/help",
      command: "/help",
      description: "Show commands.",
      category: "Commands",
    },
    { id: "exit", title: "/exit", command: "/exit", description: "Quit Smith.", category: "Commands" },
    {
      id: "new",
      title: "/new",
      command: "/new",
      description: "New session; keep history.",
      category: "Commands",
    },
    {
      id: "reload",
      title: "/reload",
      command: "/reload",
      description: "Start fresh with current context files.",
      category: "Commands",
    },
    {
      id: "init",
      title: "/init",
      command: "/init",
      description: "Create a project instruction template.",
      category: "Commands",
    },
    {
      id: "compress",
      title: "/compress",
      command: "/compress",
      description: "Persist a context summary for this session.",
      category: "Commands",
    },
    {
      id: "model",
      title: "/model",
      command: "/model",
      description: "Select or add a model profile.",
      category: "Commands",
    },
    {
      id: "config",
      title: "/config",
      command: "/config",
      description: "Edit LLM config; add `advanced` for route overrides.",
      category: "Commands",
    },
    {
      id: "sessions",
      title: "/sessions",
      command: "/sessions",
      description: "Recent sessions.",
      category: "Commands",
    },
    {
      id: "resume",
      title: "/resume",
      command: "/resume",
      description: "Resume an interrupted run or a recent session.",
      category: "Commands",
    },
    {
      id: "token",
      title: "/token",
      command: "/token",
      description: "Local token usage dashboard.",
      category: "Commands",
    },
    {
      id: "runs",
      title: "/runs",
      command: "/runs",
      description: "Recent Agent run history.",
      category: "Commands",
    },
    {
      id: "trace",
      title: "/trace",
      command: "/trace",
      description: "Diagnose the latest Agent run.",
      category: "Commands",
    },
    {
      id: "skills",
      title: "/skills",
      command: "/skills",
      description: "Inspect skills.",
      category: "Commands",
    },
    {
      id: "mcp",
      title: "/mcp",
      command: "/mcp",
      description: "Inspect MCP servers and tools.",
      category: "Commands",
    },
    {
      id: "hooks",
      title: "/hooks",
      command: "/hooks",
      description: "View runtime lifecycle hooks.",
      category: "Commands",
    },
    {
      id: "clear",
      title: "/clear",
      command: "/clear",
      description: "Delete current session.",
      category: "Commands",
    },
    {
      id: "compact",
      title: "/compact",
      command: "/compact",
      description: "Compact view.",
      category: "Commands",
    },
    {
      id: "reconnect",
      title: "/reconnect",
      command: "/reconnect",
      description: "Retry the local server connection.",
      category: "Commands",
    },
  ];

  return commands;
}

export function filterSlash(items: SlashItem[], input: string): SlashItem[] {
  if (!input.startsWith("/")) return [];

  const query = input.slice(1).trim().toLowerCase();
  if (!query) return items;
  return items.filter((item) => `${item.command} ${item.title} ${item.description}`.toLowerCase().includes(query));
}

/** Return the highlighted slash entry only while the composer still contains a partial command. */
export function selectedSlashItem(
  input: string,
  slashMenuOpen: boolean,
  items: SlashItem[],
  index: number,
): SlashItem | null {
  const selected = items[index];
  if (!slashMenuOpen || !selected || input.split(/\s+/).length !== 1 || input === selected.command) return null;
  return selected;
}

/** The welcome screen has the same composer as chat, so it must accept the first command or prompt. */
export function acceptsComposerSubmission(panel: Panel): boolean {
  return panel === "welcome" || panel === "chat";
}

export function parseSkill(raw: string, skills: SkillSummary[]): { skill: SkillSummary; prompt: string } | null {
  const match = raw.trim().match(/^\/skill\s+(\S+)(?:\s+([\s\S]+))?$/);
  if (!match) return null;

  const skill = skills.find((candidate) => candidate.name === match[1] && isSkillEnabled(candidate));
  return skill ? { skill, prompt: match[2]?.trim() || "" } : null;
}

function openConfig(args: string[], context: CommandContext): void {
  const state = context.getState();
  if (args.length > 1 || (args[0] !== undefined && args[0] !== "advanced")) {
    state.set({ statusLine: "Usage: /config [advanced]" });
    return;
  }
  const draft = createSetupDraft(state.config);
  const setupFlow = args[0] === "advanced" ? "advanced" : "initial";
  state.set({
    mode: "setup",
    setupFlow,
    setupIndex: 0,
    setupDraft: draft,
    inputValue: fieldValue(draft, setupFieldAt(0, setupFlow)),
    statusLine: setupFlow === "advanced" ? "Editing advanced config." : "Editing config.",
  });
}

async function resumeSession(args: string[], context: CommandContext): Promise<void> {
  if (args.length === 0) {
    await context.bridge.resumeRun();
    return;
  }

  if (args[0] === "run") {
    const runId = args[1];
    if (!runId || args.length !== 2) {
      context.getState().set({ statusLine: "Usage: /resume [session-id] | /resume run <run-id>" });
      return;
    }
    await context.bridge.resumeRun(runId);
    return;
  }

  if (args.length !== 1) {
    context.getState().set({ statusLine: "Usage: /resume [session-id] | /resume run <run-id>" });
    return;
  }

  const target = args[0];
  const matches = context.getState().sessions.filter((candidate) => candidate.id.startsWith(target));
  if (matches.length === 0) {
    context.getState().set({ statusLine: `Not found: ${target}` });
    return;
  }
  if (matches.length > 1) {
    context.getState().set({ statusLine: `Ambiguous session prefix: ${target}` });
    return;
  }

  const [session] = matches;
  if (session) await context.bridge.resumeSession(session);
}

const COMMAND_HANDLERS: Record<string, CommandHandler> = {
  // process.exit 由 index.tsx 的 waitUntilExit() 在 Ink 卸载后统一触发
  "/exit": (_args, context) => context.exit(),
  "/new": async (_args, context) => {
    context.bridge.startNewSession();
  },
  "/reload": async (args, context) => {
    const state = context.getState();
    if (args.length > 0) {
      state.set({ statusLine: "Usage: /reload" });
      return;
    }
    if (context.bridge.reloadContext()) {
      state.set({ statusLine: "Context reloaded. Send the next task to start fresh." });
    }
  },
  "/init": async (args, context) => {
    const state = context.getState();
    if (args.length > 0) {
      state.set({ statusLine: "Usage: /init" });
      return;
    }

    try {
      const result = await context.bridge.initializeProject(context.workingDir ?? process.cwd());
      state.set({
        statusLine: result.created
          ? `Created ${result.path}. Add your project instructions.`
          : `Already exists: ${result.path} (not changed).`,
      });
    } catch (error) {
      state.set({ statusLine: `Project initialization failed: ${errorMessage(error)}` });
    }
  },
  "/config": (args, context) => openConfig(args, context),
  "/skills": (_args, context) => {
    const state = context.getState();
    state.set({ panel: "skill-actions", inputValue: "", skillActionIndex: 0, statusLine: "Choose an action." });
  },
  "/skill": async (args, context) => {
    const state = context.getState();
    if (args.length === 0) {
      state.set({ panel: "skill-actions", inputValue: "", skillActionIndex: 0, statusLine: "Choose an action." });
      return;
    }
    const skill = state.skills.find((candidate) => candidate.name === args[0] && isSkillEnabled(candidate));
    if (!skill) {
      state.set({ statusLine: `Unknown or disabled skill: ${args[0]}` });
      return;
    }
    const prompt = args.slice(1).join(" ").trim();
    if (prompt) {
      await context.bridge.sendMessage(prompt, skill.name);
      return;
    }
    state.set({ pendingSkill: skill, panel: "chat", statusLine: "" });
  },
  "/mcp": async (_args, context) => {
    await context.bridge.refreshMcpServers();
    const state = context.getState();
    state.set({ panel: "mcp", statusLine: `${state.mcpServers.length} MCP server(s).` });
  },
  "/hooks": (_args, context) => {
    const state = context.getState();
    state.set({
      panel: "hooks",
      inputValue: "",
      hooksIndex: 0,
      statusLine: `${LIFECYCLE_HOOKS.length} built-in lifecycle hooks.`,
    });
  },
  "/model": async (args, context) => {
    const state = context.getState();
    const requested = args[0];
    if (!requested) {
      await context.bridge.openModelPicker();
      return;
    }
    if (requested === "add") {
      const [model, profileName = model, ...extra] = args.slice(1);
      if (!model || !profileName || extra.length > 0) {
        state.set({ statusLine: "Usage: /model add <model-id> [profile]." });
        return;
      }
      await context.bridge.addModelProfile(model, profileName);
      return;
    }
    await context.bridge.selectModel(requested === "default" || requested === "base" ? null : requested);
  },
  "/compress": async (_args, context) => {
    await context.bridge.compressCurrentSession();
  },
  "/sessions": (_args, context) => {
    const state = context.getState();
    state.set({ panel: "sessions", statusLine: `${state.sessions.length} session(s).` });
  },
  "/token": async (_args, context) => {
    await context.bridge.openTokenStats();
  },
  "/runs": async (_args, context) => {
    await context.bridge.openRunExplorer();
  },
  "/trace": async (args, context) => {
    if (args.length > 1) {
      context.getState().set({ statusLine: "Usage: /trace [run-id]" });
      return;
    }
    await context.bridge.showTrace(args[0]);
  },
  "/compact": (_args, context) =>
    context.getState().set({ viewMode: "compact", panel: "chat", statusLine: "Compact view." }),
  "/reconnect": async (_args, context) => {
    context.getState().set({ statusLine: "Reconnecting…", welcomeNotice: null });
    await context.bridge.boot();
  },
  "/clear": async (_args, context) => {
    await context.bridge.clearCurrentSession();
  },
  "/resume": resumeSession,
  "/help": (_args, context) => {
    const state = context.getState();
    state.pushSystemLine(HELP_TEXT);
    state.set({ panel: "chat", statusLine: "Help." });
  },
};

export async function runShellCommand(raw: string, context: CommandContext): Promise<void> {
  const [command, ...args] = raw.trim().split(/\s+/);
  const handler = command ? COMMAND_HANDLERS[command] : undefined;
  if (handler) {
    await handler(args, context);
    return;
  }

  const state = context.getState();
  const skill = state.skills.find((candidate) => candidate.name === command?.slice(1) && isSkillEnabled(candidate));
  if (!skill) {
    state.set({ statusLine: `Unknown: ${command}` });
    return;
  }

  const prompt = args.join(" ").trim();
  if (prompt) {
    await context.bridge.sendMessage(prompt, skill.name);
    return;
  }

  state.set({ pendingSkill: skill, panel: "chat", statusLine: "" });
}
