import assert from "node:assert/strict";
import test from "node:test";

import type { NodeBridge } from "./bridge.js";
import {
  acceptsComposerSubmission,
  buildSlashItems,
  filterSlash,
  parseSkill,
  runShellCommand,
  selectedSlashItem,
} from "./commands.js";
import { createAppStore } from "./store.js";

function skill(name: string) {
  return {
    name,
    description: `Run ${name}.`,
    source: "builtin",
    version: "0.1.0",
    argument_hint: "",
  };
}

test("slash filtering exposes one resume command", () => {
  const items = filterSlash(buildSlashItems(), "/");

  assert.equal(items.length, 19);
  assert.equal(
    items.some((item) => item.command === "/init"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/reconnect"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/resume"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/reload"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/skills"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/hooks"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/runs"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/trace"),
    true,
  );
  assert.equal(
    items.some((item) => item.command === "/skill"),
    false,
  );
  assert.equal(
    items.some((item) => item.command === "/run resume"),
    false,
  );
  assert.equal(
    items.some((item) => item.command === "/approve" || item.command === "/deny"),
    false,
  );
});

test("disabled skills cannot be selected through the direct skill command", () => {
  assert.equal(parseSkill("/skill research find primary sources", [{ ...skill("research"), enabled: false }]), null);
});

test("hooks opens the lifecycle hooks panel", async () => {
  const store = createAppStore();

  await runShellCommand("/hooks", { bridge: {} as NodeBridge, exit: () => {}, getState: store.getState });

  assert.equal(store.getState().panel, "hooks");
  assert.equal(store.getState().hooksIndex, 0);
  assert.match(store.getState().statusLine, /lifecycle hook/);
});

test("trace asks the bridge to diagnose an explicit run", async () => {
  const store = createAppStore();
  let requestedRunId: string | undefined;
  await runShellCommand("/trace run-123", {
    bridge: {
      showTrace: async (runId?: string) => {
        requestedRunId = runId;
      },
    } as unknown as NodeBridge,
    exit: () => {},
    getState: store.getState,
  });
  assert.equal(requestedRunId, "run-123");
});

test("trace rejects more than one argument", async () => {
  const store = createAppStore();
  await runShellCommand("/trace run-1 extra", {
    bridge: {
      showTrace: async () => {
        throw new Error("must not run");
      },
    } as unknown as NodeBridge,
    exit: () => {},
    getState: store.getState,
  });
  assert.equal(store.getState().statusLine, "Usage: /trace [run-id]");
});

test("welcome accepts the first command submitted from the composer", () => {
  assert.equal(acceptsComposerSubmission("welcome"), true);
  assert.equal(acceptsComposerSubmission("chat"), true);
  assert.equal(acceptsComposerSubmission("hooks"), false);
});

test("the highlighted hooks command is confirmed with one Enter", () => {
  const items = filterSlash(buildSlashItems(), "/");
  const hooksIndex = items.findIndex((item) => item.command === "/hooks");

  assert.equal(selectedSlashItem("/", true, items, hooksIndex)?.command, "/hooks");
});

test("resume recovers the retained or explicitly named run", async () => {
  const store = createAppStore();
  const calls: Array<string | undefined> = [];
  const bridge = {
    resumeRun: async (runId?: string) => {
      calls.push(runId);
    },
  } as unknown as NodeBridge;

  await runShellCommand("/resume", { bridge, exit: () => {}, getState: store.getState });
  await runShellCommand("/resume run run-123", { bridge, exit: () => {}, getState: store.getState });

  assert.deepEqual(calls, [undefined, "run-123"]);
});

test("resume with a session ID keeps the existing session selection behavior", async () => {
  const store = createAppStore();
  const session = { id: "session-123", agent_id: "agent-1", title: "old", created_at: "now", message_count: 1 };
  store.getState().set({ sessions: [session] });
  const calls: string[] = [];
  const bridge = {
    resumeSession: async (target: { id: string }) => {
      calls.push(target.id);
    },
  } as unknown as NodeBridge;

  await runShellCommand("/resume session-12", { bridge, exit: () => {}, getState: store.getState });

  assert.deepEqual(calls, ["session-123"]);
});

test("resume run requires an explicit run ID", async () => {
  const store = createAppStore();
  const bridge = { resumeRun: async () => {} } as unknown as NodeBridge;

  await runShellCommand("/resume run", { bridge, exit: () => {}, getState: store.getState });

  assert.equal(store.getState().statusLine, "Usage: /resume [session-id] | /resume run <run-id>");
});

test("new keeps the old session while clear delegates deletion", async () => {
  const oldSession = {
    id: "session-1",
    agent_id: "agent-1",
    title: "old",
    created_at: "now",
    message_count: 1,
  };

  const newStore = createAppStore();
  newStore.getState().set({ currentSession: oldSession, sessions: [oldSession], inputValue: "draft" });
  let newCalls = 0;
  const newBridge = {
    startNewSession: async () => {
      newCalls += 1;
      newStore.getState().startFreshSession();
      return true;
    },
  } as unknown as NodeBridge;
  await runShellCommand("/new", { bridge: newBridge, exit: () => {}, getState: newStore.getState });
  assert.equal(newCalls, 1);
  assert.equal(newStore.getState().currentSession, null);
  assert.equal(newStore.getState().sessions[0]?.id, "session-1");
  assert.deepEqual(newStore.getState().transcript, []);

  const clearStore = createAppStore();
  clearStore.getState().set({ currentSession: oldSession, sessions: [oldSession] });
  let clearCalls = 0;
  const clearBridge = {
    clearCurrentSession: async () => {
      clearCalls += 1;
      clearStore.getState().set({ sessions: [] });
      clearStore.getState().startFreshSession();
      return true;
    },
  } as unknown as NodeBridge;
  await runShellCommand("/clear", { bridge: clearBridge, exit: () => {}, getState: clearStore.getState });
  assert.equal(clearCalls, 1);
  assert.equal(clearStore.getState().currentSession, null);
  assert.deepEqual(clearStore.getState().sessions, []);
});

test("reload starts a fresh session so the next run reads the current context files", async () => {
  const oldSession = {
    id: "session-1",
    agent_id: "agent-1",
    title: "old",
    created_at: "now",
    message_count: 1,
  };
  const store = createAppStore();
  store.getState().set({ currentSession: oldSession, sessions: [oldSession], inputValue: "draft" });
  let reloadCalls = 0;
  const bridge = {
    reloadContext: () => {
      reloadCalls += 1;
      store.getState().startFreshSession();
      return true;
    },
  } as unknown as NodeBridge;

  await runShellCommand("/reload", { bridge, exit: () => {}, getState: store.getState });

  assert.equal(reloadCalls, 1);
  assert.equal(store.getState().currentSession, null);
  assert.equal(store.getState().sessions[0]?.id, "session-1");
  assert.equal(store.getState().statusLine, "Context reloaded. Send the next task to start fresh.");
});

test("reload rejects arguments", async () => {
  const store = createAppStore();
  const bridge = { reloadContext: () => true } as unknown as NodeBridge;

  await runShellCommand("/reload now", { bridge, exit: () => {}, getState: store.getState });

  assert.equal(store.getState().statusLine, "Usage: /reload");
});

test("clear keeps the current session when deletion fails", async () => {
  const store = createAppStore();
  store.getState().set({
    currentSession: { id: "session-1", agent_id: "agent-1", title: "old", created_at: "now", message_count: 1 },
  });
  const bridge = {
    clearCurrentSession: async () => false,
  } as unknown as NodeBridge;

  await runShellCommand("/clear", { bridge, exit: () => {}, getState: store.getState });

  assert.equal(store.getState().currentSession?.id, "session-1");
});

test("init command creates project instructions once and reports the preserved file", async () => {
  const store = createAppStore();
  const calls: string[] = [];
  const context = {
    bridge: {
      initializeProject: async (workingDir: string) => {
        calls.push(workingDir);
        return { path: `${workingDir}/.smith/SMITH.md`, created: calls.length === 1 };
      },
    } as unknown as NodeBridge,
    exit: () => {},
    getState: store.getState,
    workingDir: "/workspace/project",
  };

  await runShellCommand("/init", context);
  assert.match(store.getState().statusLine, /Created .+\.smith\/SMITH\.md/);

  await runShellCommand("/init", context);
  assert.match(store.getState().statusLine, /Already exists:.+not changed/);
  assert.deepEqual(calls, ["/workspace/project", "/workspace/project"]);
});

test("init command surfaces the failure reason in the status line", async () => {
  const store = createAppStore();
  const context = {
    bridge: {
      initializeProject: async () => {
        throw new Error("permission denied: .smith");
      },
    } as unknown as NodeBridge,
    exit: () => {},
    getState: store.getState,
    workingDir: "/workspace/project",
  };

  await runShellCommand("/init", context);
  assert.match(store.getState().statusLine, /Project initialization failed: permission denied: \.smith/);
});

test("model commands add relay-sharing profiles and use bridge contracts", async () => {
  const store = createAppStore();
  store.getState().set({
    config: {
      configured: true,
      has_api_key: true,
      provider: "openai",
      base_url: "https://relay.example/v1",
      model: "default-model",
      max_output_tokens: null,
      routes: {},
      timeout_profiles: {},
      models: {
        "relay-fast": { provider: "openai", model: "fast-model", has_api_key: true },
      },
    },
  });
  const calls: string[] = [];
  const bridge = {
    addModelProfile: async (model: string, name: string) => calls.push(`add:${name}:${model}`),
    selectModel: async (name: string | null) => calls.push(`model:${name}`),
    compressCurrentSession: async () => calls.push("compress"),
    refreshMcpServers: async () => calls.push("mcp"),
    openTokenStats: async () => calls.push("token"),
  } as unknown as NodeBridge;

  await runShellCommand("/model add GLM-5.2 fast", { bridge, exit: () => {}, getState: store.getState });
  await runShellCommand("/model relay-fast", { bridge, exit: () => {}, getState: store.getState });
  await runShellCommand("/compress", { bridge, exit: () => {}, getState: store.getState });
  await runShellCommand("/mcp", { bridge, exit: () => {}, getState: store.getState });
  await runShellCommand("/token", { bridge, exit: () => {}, getState: store.getState });

  assert.deepEqual(calls, ["add:fast:GLM-5.2", "model:relay-fast", "compress", "mcp", "token"]);
});

test("model opens the relay model picker", async () => {
  const store = createAppStore();
  store.getState().set({
    config: {
      configured: true,
      has_api_key: true,
      provider: "openai",
      base_url: "https://relay.example/v1",
      model: "default-model",
      max_output_tokens: null,
      routes: {},
      timeout_profiles: {},
      models: {},
    },
  });

  let opened = 0;
  const bridge = {
    openModelPicker: async () => {
      opened += 1;
    },
  } as unknown as NodeBridge;

  await runShellCommand("/model", { bridge, exit: () => {}, getState: store.getState });

  assert.equal(opened, 1);
});

test("config reopens the essential setup form with saved values intact", async () => {
  const store = createAppStore();
  store.getState().set({
    config: {
      configured: true,
      has_api_key: true,
      provider: "openai",
      base_url: "https://gateway.example/v1",
      model: "primary-model",
      max_output_tokens: null,
      routes: { gate: { model: "review-model", has_api_key: false } },
      models: {},
      timeout_profiles: {},
    },
  });

  await runShellCommand("/config", {
    bridge: {} as NodeBridge,
    exit: () => {},
    getState: store.getState,
  });

  const state = store.getState();
  assert.equal(state.mode, "setup");
  assert.equal(state.setupFlow, "initial");
  assert.equal(state.setupDraft.provider, "openai");
  assert.equal(state.setupDraft.base_url, "https://gateway.example/v1");
  assert.equal(state.setupDraft.model, "primary-model");
  assert.equal(state.setupDraft.review_model, "review-model");
  assert.equal(state.setupDraft.api_key, "");
});

test("config advanced opens the full routing form at its first field", async () => {
  const store = createAppStore();
  store.getState().set({
    config: {
      configured: true,
      has_api_key: true,
      vendor: "Relay Co",
      provider: "openai",
      base_url: "https://gateway.example/v1",
      model: "primary-model",
      max_output_tokens: null,
      routes: {},
      models: {},
      timeout_profiles: {},
    },
  });

  await runShellCommand("/config advanced", {
    bridge: {} as NodeBridge,
    exit: () => {},
    getState: store.getState,
  });

  const state = store.getState();
  assert.equal(state.mode, "setup");
  assert.equal(state.setupFlow, "advanced");
  assert.equal(state.setupIndex, 0);
  assert.equal(state.inputValue, "Relay Co");
});
