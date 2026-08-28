import { useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";
import type { StoreApi } from "zustand/vanilla";
import type { SkillSummary } from "../../../shell/src/api.js";
import type { NodeBridge } from "../../../shell/src/bridge.js";
import { buildSlashItems, filterSlash, runShellCommand, type SlashItem } from "../../../shell/src/commands.js";
import { filterSkillMentions, isSkillMentionQuery } from "../../../shell/src/skill-mention.js";
import type { AppStore } from "../../../shell/src/store.js";
import type { TranscriptEntry, TurnBlock } from "../../../shell/src/transcript-state.js";
import { Markdown } from "./Markdown.tsx";
import { SmithUiBlock } from "./SmithUi.tsx";
import { Sidebar, when } from "./Sidebar.tsx";

type Props = { store: StoreApi<AppStore>; bridge: NodeBridge };

type Tab = { key: string; sessionId: string | null; title: string };

export function App({ store, bridge }: Props) {
  const state = useStore(store);
  const [draft, setDraft] = useState("");
  const [menu, setMenu] = useState(false);
  const [sideOpen, setSideOpen] = useState(false);
  const [drawer, setDrawer] = useState(false);
  // A tab is local and exists before any session does: Smith creates the
  // session lazily on the first message, so binding tabs to currentSession
  // left a brand-new window with no tab at all.
  const [tabs, setTabs] = useState<Tab[]>([{ key: "t0", sessionId: null, title: "新会话" }]);
  const [activeKey, setActiveKey] = useState("t0");
  const [picked, setPicked] = useState(0);
  const endRef = useRef<HTMLDivElement>(null);
  const boxRef = useRef<HTMLTextAreaElement>(null);

  // The palettes are derived, not stored: shell already owns the matching rules,
  // so re-running them per keystroke keeps one source of truth.
  const slashItems = useMemo(() => buildSlashItems(), []);
  const slashHits = draft.startsWith("/") ? filterSlash(slashItems, draft) : [];
  const skillHits = isSkillMentionQuery(draft) ? filterSkillMentions(state.skills, draft) : [];
  const options: (SlashItem | SkillSummary)[] = slashHits.length ? slashHits : skillHits;
  const paletteOpen = options.length > 0;

  useEffect(() => setPicked(0), [draft]);

  const current = state.currentSession;
  // Adopt the session into whichever tab is active once the backend creates it.
  useEffect(() => {
    if (!current) return;
    setTabs((prev) => {
      if (prev.some((tab) => tab.sessionId === current.id)) {
        return prev.map((tab) =>
          tab.sessionId === current.id ? { ...tab, title: current.title || tab.title } : tab,
        );
      }
      return prev.map((tab) =>
        tab.key === activeKey ? { ...tab, sessionId: current.id, title: current.title || tab.title } : tab,
      );
    });
  }, [current, activeKey]);

  // A command that opens a panel should reveal the sidebar; leaving it closed
  // was why /token and /runs looked like they did nothing.
  useEffect(() => {
    if (state.panel !== "chat" && state.panel !== "welcome") setSideOpen(true);
  }, [state.panel]);

  function openTab() {
    const key = `t${Date.now()}`;
    setTabs((prev) => [...prev, { key, sessionId: null, title: "新会话" }]);
    setActiveKey(key);
    bridge.startNewSession();
  }

  function pickTab(tab: Tab) {
    if (tab.key === activeKey) return;
    setActiveKey(tab.key);
    const session = tab.sessionId ? state.sessions.find((item) => item.id === tab.sessionId) : null;
    if (session) void bridge.resumeSession(session);
    else bridge.startNewSession();
  }

  function closeTab(tab: Tab) {
    setTabs((prev) => {
      const next = prev.filter((item) => item.key !== tab.key);
      if (!next.length) return [{ key: "t0", sessionId: null, title: "新会话" }];
      if (tab.key === activeKey) {
        const fallback = next[next.length - 1];
        setActiveKey(fallback.key);
        const session = fallback.sessionId ? state.sessions.find((s) => s.id === fallback.sessionId) : null;
        if (session) void bridge.resumeSession(session);
      }
      return next;
    });
  }

  const atBottomRef = useRef(true);
  useEffect(() => {
    if (atBottomRef.current) endRef.current?.scrollIntoView({ block: "end" });
  }, [state.transcript, state.pendingApproval]);

  // Grow the composer with its content instead of scrolling a fixed 3 rows.
  useEffect(() => {
    const el = boxRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [draft]);

  function accept() {
    const chosen = options[picked];
    if (!chosen) return;
    // SlashItem carries `command`; SkillSummary carries `name`.
    const next = "command" in chosen ? `${chosen.command} ` : `@${chosen.name} `;
    setDraft(next);
    boxRef.current?.focus();
  }

  function submit() {
    const text = draft.trim();
    if (!text || state.inputLocked) return;
    setDraft("");
    if (text.startsWith("/")) {
      // shell owns all 21 commands; desktop only supplies the context.
      void runShellCommand(text, {
        bridge,
        exit: () => window.close(),
        getState: () => store.getState(),
        workingDir: process.cwd(),
      });
      return;
    }
    void bridge.sendMessage(text);
  }

  const modelLabel =
    state.selectedModelProfile ?? state.config?.routes?.interactive?.model ?? state.config?.model ?? "未配置";

  return (
    <>
      <div className="tabbar">
        <div className="lights" />
        <button type="button" className="icon" title="会话列表" onClick={() => setDrawer((v) => !v)}>
          <GridIcon />
        </button>
        <div className="tabs">
          {tabs.map((tab) => (
            <div
              key={tab.key}
              className={`tab ${tab.key === activeKey ? "active" : ""}`}
              onClick={() => pickTab(tab)}
              onKeyDown={(event) => event.key === "Enter" && pickTab(tab)}
              role="button"
              tabIndex={0}
            >
              <span className="mark">{(tab.title || "S").slice(0, 1).toUpperCase()}</span>
              <span className="label">{tab.title}</span>
              <button
                type="button"
                className="close"
                title="关闭标签页"
                onClick={(event) => {
                  event.stopPropagation();
                  closeTab(tab);
                }}
              >
                ×
              </button>
            </div>
          ))}
          <button type="button" className="icon" title="新建会话" onClick={openTab}>
            +
          </button>
        </div>
        <div className="spacer" />
        <button
          type="button"
          className={`icon ${sideOpen ? "on" : ""}`}
          title="侧边栏"
          onClick={() => setSideOpen((v) => !v)}
        >
          <PanelIcon />
        </button>
      </div>

      {drawer ? (
        <>
          <div className="scrim" onClick={() => setDrawer(false)} role="presentation" />
          <div className="drawer">
            {state.sessions.slice(0, 40).map((session) => (
              <button
                key={session.id}
                type="button"
                className="drawer-item"
                onClick={() => {
                  setDrawer(false);
                  void bridge.resumeSession(session);
                }}
              >
                <span className="dt">{session.title || "未命名"}</span>
                <span className="dm">
                  {session.message_count} 条 · {when(session.last_message_at ?? session.created_at)}
                </span>
              </button>
            ))}
            {state.sessions.length === 0 ? <div className="drawer-item dim">暂无历史会话</div> : null}
          </div>
        </>
      ) : null}

      <div className="body">
      <div className="main">
      <div className="sessionbar">
        <span className="title">{current?.title ?? tabs.find((t) => t.key === activeKey)?.title ?? "新会话"}</span>
        <div className="spacer" />
        {state.busy || state.compressing ? <span className="spin" /> : null}
        <span className="menu">
          <button type="button" className="icon" title="更多" onClick={() => setMenu((v) => !v)}>
            ⋯
          </button>
          {menu ? (
            <>
              <div className="scrim bare" onClick={() => setMenu(false)} role="presentation" />
              <div className="popup right">
                {(
                  [
                    ["新建会话", () => openTab()],
                    ["压缩上下文", () => void bridge.compressCurrentSession()],
                    ["清空当前会话", () => void bridge.clearCurrentSession()],
                    ["重新加载上下文", () => bridge.reloadContext()],
                    ["用量统计", () => void bridge.openTokenStats()],
                    ["运行记录", () => void bridge.openRunExplorer()],
                    ["技能", () => void bridge.refreshSkills()],
                    ["MCP 服务", () => void bridge.refreshMcpServers()],
                  ] as [string, () => void][]
                ).map(([label, run]) => (
                  <button
                    key={label}
                    type="button"
                    onClick={() => {
                      setMenu(false);
                      run();
                    }}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </>
          ) : null}
        </span>
      </div>

      <main
        className="transcript"
        onScroll={(event) => {
          const el = event.currentTarget;
          atBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
      >
        <div className="column">
          {state.transcript.map((entry) => (
            <Entry key={entry.id} entry={entry} />
          ))}
          {state.pendingApproval ? (
            <Approval
              approval={state.pendingApproval}
              resolving={state.approvalResolving}
              onResolve={(ok) => void bridge.resolveApproval(ok)}
            />
          ) : null}
          <div ref={endRef} />
        </div>
      </main>

      <footer className="composer">
        <div className="column">
          {paletteOpen ? (
            <div className="palette">
              {options.slice(0, 10).map((option, index) => {
                const isSlash = "command" in option;
                return (
                  <button
                    key={isSlash ? option.id : option.name}
                    type="button"
                    className={`palette-item ${index === picked ? "on" : ""}`}
                    onMouseEnter={() => setPicked(index)}
                    onClick={accept}
                  >
                    <span className="k">{isSlash ? option.command : `@${option.name}`}</span>
                    <span className="d">{isSlash ? option.description : (option.description ?? "")}</span>
                  </button>
                );
              })}
            </div>
          ) : null}
          <div className="box">
            <textarea
              ref={boxRef}
              autoFocus
              rows={1}
              value={draft}
              placeholder="随便问点什么，/ 可查看命令，@ 可添加上下文..."
              disabled={state.inputLocked}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (paletteOpen && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
                  event.preventDefault();
                  setPicked((v) => (v + (event.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
                  return;
                }
                if (paletteOpen && (event.key === "Tab" || (event.key === "Enter" && !event.nativeEvent.isComposing))) {
                  event.preventDefault();
                  accept();
                  return;
                }
                if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                  event.preventDefault();
                  submit();
                }
                if (event.key === "Escape") {
                  if (paletteOpen) setDraft("");
                  else if (state.busy) bridge.cancelRequest();
                }
              }}
            />
            <div className="toolbar">
              <button type="button" className="icon" title="添加上下文">
                +
              </button>
              <ModelMenu
                label={modelLabel}
                profiles={Object.keys(state.config?.models ?? {})}
                onPick={(name) => void bridge.selectModel(name)}
              />
              <span className="chip dim">{state.pendingSkill?.name ?? "Default"}</span>
              <div className="spacer" />
              <button
                type="button"
                className={`send ${draft.trim() ? "on" : ""}`}
                title={state.busy ? "运行中 — Esc 取消" : "发送"}
                onClick={submit}
                disabled={state.inputLocked || !draft.trim()}
              >
                ↑
              </button>
            </div>
          </div>
          <div className="status">
            {state.busy ? <span className="dot" /> : null}
            <span>{state.statusLine}</span>
            <div className="spacer" />
            {state.queuedMessages.length > 0 ? <span>队列 {state.queuedMessages.length}</span> : null}
            {state.contextUsage?.context_tokens ? (
              <span>{Math.round(state.contextUsage.context_percent)}% 上下文</span>
            ) : null}
          </div>
        </div>
      </footer>
      </div>
      {sideOpen ? <Sidebar state={state} bridge={bridge} onClose={() => setSideOpen(false)} /> : null}
      </div>
    </>
  );
}

function ModelMenu({
  label,
  profiles,
  onPick,
}: {
  label: string;
  profiles: string[];
  onPick: (name: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <span className="menu">
      <button type="button" className="chip" onClick={() => setOpen((v) => !v)}>
        {label} ⌄
      </button>
      {open && profiles.length > 0 ? (
        <div className="popup">
          {profiles.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => {
                setOpen(false);
                onPick(name);
              }}
            >
              {name}
            </button>
          ))}
        </div>
      ) : null}
    </span>
  );
}

function Entry({ entry }: { entry: TranscriptEntry }) {
  if (entry.kind === "system") {
    return <div className={`entry system ${entry.tone === "error" ? "error" : ""}`}>{entry.text}</div>;
  }
  return (
    <div className="entry">
      {entry.userText ? <div className="user">{entry.userText}</div> : null}
      {entry.blocks.map((block) => (
        <Block key={block.id} block={block} />
      ))}
      {entry.assistantText ? <Markdown text={entry.assistantText} /> : null}
      {entry.provisional.map((item) => (
        <div key={item.provisionId} className="provisional">
          <Markdown text={item.text} />
        </div>
      ))}
    </div>
  );
}

function Block({ block }: { block: TurnBlock }) {
  switch (block.type) {
    case "thinking":
      return <div className="block think">{block.text}</div>;
    case "tool":
      return (
        <div className={`block tool ${block.state}`}>
          <span className="tool-name">{block.name}</span>
          {block.hint ? <span className="tool-hint">{block.hint}</span> : null}
          {block.summary ? <div className="tool-summary">{block.summary}</div> : null}
        </div>
      );
    case "skill":
      return (
        <div className={`block skill ${block.state}`}>
          <span className="tool-name">skill: {block.name}</span>
          <span className="tool-hint">{block.state}</span>
          {block.activities.map((activity) => (
            <div key={activity.id} className="tool-summary">
              {activity.name} {activity.hint}
            </div>
          ))}
        </div>
      );
    case "smith_ui":
      // 这个分支此前不存在, 于是结构化载荷掉进下面的 default 被 JSON.stringify
      // 整坨倒出来 —— 终端那侧一直是渲染成组件的。
      return <SmithUiBlock payload={block.payload} />;
    case "smith_ui_fallback":
      return <div className="block">[smith-ui 无法渲染: {block.reason}]</div>;
    default:
      return <div className="block">{JSON.stringify(block, null, 2)}</div>;
  }
}

function Approval({
  approval,
  resolving,
  onResolve,
}: {
  approval: NonNullable<AppStore["pendingApproval"]>;
  resolving: boolean;
  onResolve: (approved: boolean) => void;
}) {
  const presentation = approval.presentation;
  return (
    <div className="approval">
      <h4>{presentation?.title ?? `${approval.tool} 需要批准`}</h4>
      <pre>{presentation?.summary ?? JSON.stringify(approval.arguments, null, 2)}</pre>
      {approval.reason ? <p className="dim">原因：{approval.reason}</p> : null}
      <div>
        <button type="button" className="primary" disabled={resolving} onClick={() => onResolve(true)}>
          批准
        </button>
        <button type="button" disabled={resolving} onClick={() => onResolve(false)}>
          拒绝
        </button>
      </div>
    </div>
  );
}

const GridIcon = () => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="currentColor" aria-hidden="true">
    <rect x="1" y="1" width="5.5" height="5.5" rx="1.4" />
    <rect x="8.5" y="1" width="5.5" height="5.5" rx="1.4" />
    <rect x="1" y="8.5" width="5.5" height="5.5" rx="1.4" />
    <rect x="8.5" y="8.5" width="5.5" height="5.5" rx="1.4" />
  </svg>
);

const PanelIcon = () => (
  <svg width="15" height="15" viewBox="0 0 15 15" fill="none" stroke="currentColor" strokeWidth="1.4" aria-hidden="true">
    <rect x="1" y="2" width="13" height="11" rx="2" />
    <line x1="9.5" y1="2" x2="9.5" y2="13" />
  </svg>
);
