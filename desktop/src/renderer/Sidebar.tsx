import { useEffect, useRef } from "react";
import type { NodeBridge } from "../../../shell/src/bridge.js";
import { LIFECYCLE_HOOKS } from "../../../shell/src/hooks.js";
import type { AppStore, Panel } from "../../../shell/src/store.js";
import { TOKEN_TABS } from "../../../shell/src/token-stats.js";

const TITLES: Partial<Record<Panel, string>> = {
  sessions: "会话",
  skills: "技能",
  "skill-actions": "技能",
  "skill-toggle": "技能",
  mcp: "MCP 服务",
  hooks: "生命周期钩子",
  "hook-details": "生命周期钩子",
  tokens: "用量",
  runs: "运行记录",
};

/**
 * The commands already load their data into the store — `/token` fills
 * tokenStats, `/runs` fills observabilityRuns. Before this existed they wrote
 * state nothing rendered, so the command looked broken.
 */
export function Sidebar({
  state,
  bridge,
  onClose,
}: {
  state: AppStore;
  bridge: NodeBridge;
  onClose: () => void;
}) {
  const panel = state.panel === "chat" || state.panel === "welcome" ? "sessions" : state.panel;
  return (
    <aside className="sidebar">
      <header>
        <span className="t">{TITLES[panel] ?? "面板"}</span>
        <div className="spacer" />
        <button type="button" className="icon" onClick={onClose} title="关闭">
          ×
        </button>
      </header>
      <div className="panel-body">
        <Body state={state} bridge={bridge} panel={panel} />
      </div>
    </aside>
  );
}

function Body({ state, bridge, panel }: { state: AppStore; bridge: NodeBridge; panel: Panel }) {
  switch (panel) {
    case "sessions":
      return (
        <List
          rows={state.sessions.map((session) => ({
            key: session.id,
            title: session.title || "未命名",
            meta: `${session.message_count} 条 · ${when(session.last_message_at ?? session.created_at)}`,
            sub: session.last_message_preview ?? undefined,
            onClick: () => void bridge.resumeSession(session),
          }))}
          empty="暂无会话"
        />
      );

    case "skills":
    case "skill-actions":
    case "skill-toggle":
      return (
        <List
          rows={state.skills.map((skill) => ({
            key: skill.name,
            title: skill.name,
            meta: skill.enabled === false ? "已停用" : "启用中",
            sub: skill.description,
            onClick: () => void bridge.setSkillEnabled(skill.name, skill.enabled === false),
          }))}
          empty="未安装技能"
        />
      );

    case "mcp":
      return (
        <List
          rows={state.mcpServers.map((server) => ({
            key: server.name,
            title: server.name,
            meta: `${server.status === "connected" ? "已连接" : "错误"} · ${server.tools.length} 个工具`,
            sub: server.error ?? server.url ?? server.command.join(" "),
            tone: server.status === "connected" ? "ok" : "error",
          }))}
          empty="未配置 MCP 服务"
        />
      );

    case "hooks":
    case "hook-details":
      return (
        <List
          rows={LIFECYCLE_HOOKS.map((hook) => ({
            key: hook.event,
            title: hook.event,
            meta: hook.handler,
            sub: hook.detail,
          }))}
          empty="无钩子"
        />
      );

    case "tokens":
      return <Tokens state={state} bridge={bridge} />;

    case "runs":
      return <Runs state={state} bridge={bridge} />;

    default:
      return <p className="empty">没有可显示的面板。</p>;
  }
}

function Tokens({ state, bridge }: { state: AppStore; bridge: NodeBridge }) {
  const stats = state.tokenStats;
  useLoadOnce(!stats, () => void bridge.openTokenStats());
  if (!stats) return <p className="empty">正在加载用量…</p>;
  return (
    <>
      <div className="tabs-row">
        {TOKEN_TABS.map((tab) => (
          <button
            key={tab}
            type="button"
            className={`pill ${state.tokenTab === tab ? "on" : ""}`}
            onClick={() => state.set({ tokenTab: tab })}
          >
            {tab === "overview" ? "概览" : tab === "models" ? "模型" : "统计"}
          </button>
        ))}
        <div className="spacer" />
        <button type="button" className="pill" onClick={() => void bridge.openTokenStats()}>
          刷新
        </button>
      </div>

      {state.tokenTab === "models" ? (
        <List
          rows={stats.models.map((model) => ({
            key: model.model,
            title: model.model,
            meta: n(model.total_tokens),
            sub: `输入 ${n(model.input_tokens)} · 输出 ${n(model.output_tokens)} · ${model.sessions} 会话`,
          }))}
          empty="无模型用量"
        />
      ) : state.tokenTab === "stats" ? (
        <Facts
          rows={[
            ["活跃天数", String(stats.active_days)],
            ["当前连续", `${stats.current_streak} 天`],
            ["最长连续", `${stats.longest_streak} 天`],
            ["常用模型", stats.favorite_model ?? "—"],
            ["高峰时段", stats.peak_hour === null ? "—" : `${stats.peak_hour}:00`],
            ["会话总数", String(stats.session_count)],
          ]}
        />
      ) : (
        <>
          <Facts
            rows={[
              [`${stats.year} 年总计`, n(stats.total_tokens) + (stats.estimated ? "（估算）" : "")],
              ["输入", n(stats.input_tokens)],
              ["输出", n(stats.output_tokens)],
            ]}
          />
          <List
            rows={stats.daily.slice(-14).reverse().map((day) => ({
              key: day.date,
              title: day.date,
              meta: n(day.total_tokens),
              sub: `${day.sessions} 会话`,
            }))}
            empty="无每日数据"
          />
        </>
      )}
    </>
  );
}

function Runs({ state, bridge }: { state: AppStore; bridge: NodeBridge }) {
  const health = state.observabilityHealth;
  const runs = state.observabilityRuns;
  const incidents = state.observabilityIncidents ?? [];
  useLoadOnce(!runs, () => void bridge.openRunExplorer());
  if (!runs) return <p className="empty">正在加载运行记录…</p>;
  return (
    <>
      {health ? (
        <Facts
          rows={[
            ["运行次数", String(health.run_count)],
            ["成功率", `${Math.round(health.success_rate * 100)}%`],
            ["工具调用", String(health.tool_call_count)],
            ["每次 token", n(Math.round(health.tokens_per_run))],
          ]}
        />
      ) : null}
      {incidents.length ? (
        <List
          rows={incidents.slice(0, 8).map((incident) => ({
            key: `${incident.run_id}-${incident.occurred_at}`,
            title: incident.category,
            meta: incident.severity === "error" ? "错误" : "警告",
            sub: incident.message,
            tone: incident.severity === "error" ? "error" : undefined,
          }))}
          empty=""
        />
      ) : null}
      <List
        rows={runs.slice(0, 30).map((run) => ({
          key: run.run_id,
          title: run.run_id.slice(0, 8),
          meta: `${run.outcome ?? "?"} · ${when(run.created_at)}`,
          sub: `${run.event_count} 事件 · ${run.tool_call_count} 工具${run.forced_skill ? ` · ${run.forced_skill}` : ""}`,
          tone: run.outcome === "completed" ? "ok" : run.outcome ? "error" : undefined,
        }))}
        empty="无运行记录"
      />
    </>
  );
}

/** Fires the fetch once per mount; the panel must not re-request on every render. */
function useLoadOnce(needed: boolean, load: () => void) {
  const fired = useRef(false);
  useEffect(() => {
    if (!needed || fired.current) return;
    fired.current = true;
    load();
  }, [needed, load]);
}

type Row = {
  key: string;
  title: string;
  meta?: string;
  sub?: string;
  tone?: "ok" | "error";
  onClick?: () => void;
};

function List({ rows, empty }: { rows: Row[]; empty: string }) {
  if (!rows.length) return empty ? <p className="empty">{empty}</p> : null;
  return (
    <div className="rows">
      {rows.map((row) => (
        <div
          key={row.key}
          className={`row ${row.onClick ? "click" : ""}`}
          onClick={row.onClick}
          onKeyDown={(event) => row.onClick && event.key === "Enter" && row.onClick()}
          role={row.onClick ? "button" : undefined}
          tabIndex={row.onClick ? 0 : undefined}
        >
          <div className="head">
            <span className="name">{row.title}</span>
            {row.meta ? <span className={`meta ${row.tone ?? ""}`}>{row.meta}</span> : null}
          </div>
          {row.sub ? <div className="sub">{row.sub}</div> : null}
        </div>
      ))}
    </div>
  );
}

function Facts({ rows }: { rows: [string, string][] }) {
  return (
    <div className="facts">
      {rows.map(([label, value]) => (
        <div key={label}>
          <span>{label}</span>
          <b>{value}</b>
        </div>
      ))}
    </div>
  );
}

function n(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}k`;
  return String(value);
}

export function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const mins = Math.floor((Date.now() - then) / 60000);
  if (mins < 1) return "刚刚";
  if (mins < 60) return `${mins} 分钟前`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  return days < 30 ? `${days} 天前` : new Date(then).toLocaleDateString("zh-CN");
}
