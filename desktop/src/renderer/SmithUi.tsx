/**
 * DOM 渲染器：把 `smith_ui` 的声明式规格画成 HTML。
 *
 * 终端那侧走 `shell/src/smith-ui.tsx`（Ink + `@json-render/ink`），Ink 的
 * `<Box>/<Text>` 只能渲染到 TTY，DOM 里用不了；而 `@json-render` 家族这边只装了
 * core 和 ink，没有 DOM 渲染器包。所以这一份是手写的，组件集合与引擎白名单
 * （`engine/execution/react/smith_ui.py` 的 `_ALLOWED_COMPONENTS`，18 个）对齐。
 *
 * **宽容读取**：`parseSmithUiPayload()` 只校验结构（组件名、深度、元素数、
 * 字符串长度），**props 原样透传不做校验**。实测模型发出来的 props 和
 * `@json-render/ink` catalog 的词汇对不上：ProgressBar 发 `value` 而 catalog 要
 * `progress`，KeyValue 发 `entries[]` 而 catalog 要 `label`/`value`，
 * StatusLine 发 `label`+`detail` 而 catalog 要 `text`，BarChart 发
 * `labels[]`+`values[]` 而 catalog 要 `data[]`。两套都接，否则真实载荷渲染出来
 * 是一片空白 —— 那和现在倒一坨 JSON 相比只是换了种难看。
 */

import type { SmithUiElement, SmithUiPayload, SmithUiSpec } from "../../../shell/src/smith-ui-schema.js";

/** 终端色名 → 本应用的调色板。认不出的色名交给 CSS（它可能本来就是合法 CSS 色）。 */
const COLORS: Record<string, string> = {
  green: "var(--ok)",
  red: "var(--error)",
  yellow: "#e0b341",
  cyan: "#4ec9d0",
  blue: "var(--accent)",
  magenta: "#c586c0",
  gray: "var(--dim)",
  grey: "var(--dim)",
  white: "var(--fg)",
};

const color = (name: unknown): string | undefined =>
  typeof name === "string" ? (COLORS[name.toLowerCase()] ?? name) : undefined;

const str = (v: unknown): string => (typeof v === "string" ? v : typeof v === "number" ? String(v) : "");
const num = (v: unknown): number | undefined => (typeof v === "number" && Number.isFinite(v) ? v : undefined);

/** 0..1 归一。模型有时直接发 0..100，>1 的值按百分数理解。 */
function ratio(...candidates: unknown[]): number {
  for (const c of candidates) {
    const n = num(c);
    if (n === undefined) continue;
    const v = n > 1 ? n / 100 : n;
    return Math.min(1, Math.max(0, v));
  }
  return 0;
}

/** StatusLine / Callout 的图标，与终端默认保持一致。 */
const STATUS_ICON: Record<string, string> = {
  info: "ℹ",
  success: "✔",
  warning: "⚠",
  error: "✖",
  tip: "💡",
  note: "ℹ",
};

const STATUS_COLOR: Record<string, string> = {
  info: "var(--accent)",
  success: "var(--ok)",
  warning: "#e0b341",
  error: "var(--error)",
  tip: "#4ec9d0",
  note: "var(--dim)",
};

type Props = Record<string, unknown>;

function Text_({ p }: { p: Props }) {
  const style: React.CSSProperties = {
    color: color(p.color),
    fontWeight: p.bold === true ? 600 : undefined,
    fontStyle: p.italic === true ? "italic" : undefined,
    textDecoration: p.underline === true ? "underline" : undefined,
    opacity: p.dimColor === true ? 0.6 : undefined,
  };
  return <span style={style}>{str(p.text)}</span>;
}

function Heading_({ p }: { p: Props }) {
  // catalog 用 "h1".."h4"，模型也会发数字 3。两种都认。
  const raw = p.level;
  const level = typeof raw === "number" ? raw : Number(String(raw ?? "h2").replace(/^h/i, "")) || 2;
  return (
    <div className={`sui-heading sui-h${Math.min(4, Math.max(1, level))}`}>{str(p.text)}</div>
  );
}

function Divider_({ p }: { p: Props }) {
  const title = str(p.title);
  return (
    <div className="sui-divider" style={{ borderColor: color(p.color) }}>
      {title ? <span className="sui-divider-title">{title}</span> : null}
    </div>
  );
}

function Badge_({ p }: { p: Props }) {
  const variant = str(p.variant) || "info";
  return (
    <span className="sui-badge" style={{ color: STATUS_COLOR[variant] ?? color(p.color) }}>
      {str(p.label) || str(p.text)}
    </span>
  );
}

function ProgressBar_({ p }: { p: Props }) {
  // catalog: progress；实测模型发: value。
  const value = ratio(p.progress, p.value);
  const label = str(p.label);
  return (
    <div className="sui-progress">
      {label ? <span className="sui-progress-label">{label}</span> : null}
      <span className="sui-progress-track">
        <span
          className="sui-progress-fill"
          style={{ width: `${(value * 100).toFixed(1)}%`, background: color(p.color) ?? "var(--accent)" }}
        />
      </span>
      <span className="sui-progress-pct">{Math.round(value * 100)}%</span>
    </div>
  );
}

function Sparkline_({ p }: { p: Props }) {
  const data = Array.isArray(p.data) ? p.data.map(num).filter((n): n is number => n !== undefined) : [];
  if (data.length === 0) return null;
  const lo = num(p.min) ?? Math.min(...data);
  const hi = num(p.max) ?? Math.max(...data);
  const span = hi - lo || 1;
  const label = str(p.label);
  return (
    <div className="sui-sparkline">
      {label ? <span className="sui-kv-label">{label}</span> : null}
      <span className="sui-spark-bars" style={{ color: color(p.color) ?? "var(--accent)" }}>
        {data.map((v, i) => (
          // 用高度而不是 Unicode 方块：DOM 里有真实像素，不必受 8 级量化限制。
          <span key={i} className="sui-spark-bar" style={{ height: `${6 + ((v - lo) / span) * 18}px` }} />
        ))}
      </span>
    </div>
  );
}

function BarChart_({ p }: { p: Props }) {
  // catalog: data[{label,value,color}]；实测模型发: labels[] + values[]。
  const rows: { label: string; value: number; color?: string | undefined }[] = [];
  if (Array.isArray(p.data)) {
    for (const raw of p.data) {
      const item = (raw ?? {}) as Props;
      rows.push({ label: str(item.label), value: num(item.value) ?? 0, color: color(item.color) });
    }
  } else if (Array.isArray(p.labels) && Array.isArray(p.values)) {
    p.labels.forEach((l, i) => {
      rows.push({ label: str(l), value: num((p.values as unknown[])[i]) ?? 0, color: undefined });
    });
  }
  if (rows.length === 0) return null;

  const max = Math.max(...rows.map((r) => r.value), 1);
  const total = rows.reduce((sum, r) => sum + r.value, 0) || 1;
  const unit = str(p.unit);
  const title = str(p.title);
  return (
    <div className="sui-chart">
      {title ? <div className="sui-chart-title">{title}</div> : null}
      {rows.map((r, i) => (
        <div key={i} className="sui-chart-row">
          <span className="sui-chart-label">{r.label}</span>
          <span className="sui-chart-track">
            <span
              className="sui-chart-fill"
              style={{ width: `${(r.value / max) * 100}%`, background: r.color ?? "var(--accent)" }}
            />
          </span>
          <span className="sui-chart-value">
            {p.showPercentage === true ? `${Math.round((r.value / total) * 100)}%` : `${r.value}${unit}`}
          </span>
        </div>
      ))}
    </div>
  );
}

function Table_({ p }: { p: Props }) {
  const columns = Array.isArray(p.columns) ? (p.columns as Props[]) : [];
  const rows = Array.isArray(p.rows) ? (p.rows as Props[]) : [];
  if (columns.length === 0) return null;
  return (
    <div className="sui-table-wrap">
      <table className="sui-table">
        <thead>
          <tr>
            {columns.map((c, i) => (
              <th key={i} style={{ color: color(p.headerColor) }}>
                {str(c.header) || str(c.key)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri}>
              {columns.map((c, ci) => (
                <td key={ci}>{str(row[str(c.key)])}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function List_({ p }: { p: Props }) {
  const items = Array.isArray(p.items) ? p.items.map(str) : [];
  if (items.length === 0) return null;
  const Tag = p.ordered === true ? "ol" : "ul";
  return (
    <Tag className="sui-list">
      {items.map((item, i) => (
        <li key={i}>{item}</li>
      ))}
    </Tag>
  );
}

function ListItem_({ p }: { p: Props }) {
  return (
    <div className="sui-listitem">
      {str(p.leading) ? <span className="sui-li-lead">{str(p.leading)}</span> : null}
      <span className="sui-li-body">
        <span className="sui-li-title">{str(p.title)}</span>
        {str(p.subtitle) ? <span className="sui-li-sub">{str(p.subtitle)}</span> : null}
      </span>
      {str(p.trailing) ? <span className="sui-li-trail">{str(p.trailing)}</span> : null}
    </div>
  );
}

function KeyValue_({ p }: { p: Props }) {
  // catalog: 单条 label/value；实测模型发: entries[{key,value}] 多条。
  const pairs: { k: string; v: string }[] = [];
  if (Array.isArray(p.entries)) {
    for (const raw of p.entries) {
      const e = (raw ?? {}) as Props;
      pairs.push({ k: str(e.key) || str(e.label), v: joinValue(e.value) });
    }
  } else if (p.label !== undefined || p.value !== undefined) {
    pairs.push({ k: str(p.label), v: joinValue(p.value) });
  }
  if (pairs.length === 0) return null;
  const sep = typeof p.separator === "string" ? p.separator : "：";
  return (
    <div className="sui-kv">
      {pairs.map((pair, i) => (
        <div key={i} className="sui-kv-row">
          <span className="sui-kv-label" style={{ color: color(p.labelColor) }}>
            {pair.k}
            {sep}
          </span>
          <span className="sui-kv-value">{pair.v}</span>
        </div>
      ))}
    </div>
  );
}

/** value 允许是 string | number | string[]（数组用逗号连接，与终端一致）。 */
function joinValue(value: unknown): string {
  return Array.isArray(value) ? value.map(str).filter(Boolean).join(", ") : str(value);
}

function StatusLine_({ p }: { p: Props }) {
  const status = (str(p.status) || "info").toLowerCase();
  // catalog: text；实测模型发: label + detail。
  const head = str(p.text) || str(p.label);
  const detail = str(p.detail);
  return (
    <div className="sui-status">
      <span className="sui-status-icon" style={{ color: STATUS_COLOR[status] ?? "var(--dim)" }}>
        {str(p.icon) || STATUS_ICON[status] || "•"}
      </span>
      <span>{head}</span>
      {detail ? <span className="sui-status-detail">{detail}</span> : null}
    </div>
  );
}

function Metric_({ p }: { p: Props }) {
  const trend = str(p.trend).toLowerCase();
  const arrow = trend === "up" ? "▲" : trend === "down" ? "▼" : "";
  return (
    <div className="sui-metric">
      <span className="sui-metric-label">{str(p.label)}</span>
      <span className="sui-metric-value">
        {str(p.value)}
        {arrow ? (
          <span
            className="sui-metric-trend"
            style={{ color: trend === "up" ? "var(--ok)" : "var(--error)" }}
          >
            {arrow}
          </span>
        ) : null}
      </span>
      {str(p.detail) ? <span className="sui-metric-detail">{str(p.detail)}</span> : null}
    </div>
  );
}

function Callout_({ p }: { p: Props }) {
  const type = (str(p.type) || "note").toLowerCase();
  return (
    <div className="sui-callout" style={{ borderLeftColor: STATUS_COLOR[type] ?? "var(--line)" }}>
      {str(p.title) ? <div className="sui-callout-title">{str(p.title)}</div> : null}
      <div className="sui-callout-body">{str(p.content) || str(p.text)}</div>
    </div>
  );
}

/** Box 的 flex 几何。terminal 的 1 格 ≈ 8px，padding/gap 按这个换算。 */
function boxStyle(p: Props): React.CSSProperties {
  const cell = (v: unknown, scale = 8): string | undefined => {
    const n = num(v);
    return n === undefined ? undefined : `${n * scale}px`;
  };
  return {
    display: "flex",
    flexDirection: p.flexDirection === "column" ? "column" : "row",
    alignItems: typeof p.alignItems === "string" ? p.alignItems : undefined,
    justifyContent: typeof p.justifyContent === "string" ? p.justifyContent : undefined,
    gap: cell(p.gap),
    padding: cell(p.padding),
    marginTop: cell(p.marginTop),
    marginBottom: cell(p.marginBottom),
    ...(typeof p.borderStyle === "string" && p.borderStyle !== "none"
      ? { border: "1px solid var(--line)", borderRadius: 8 }
      : {}),
  };
}

/**
 * 递归渲染一个元素。
 *
 * `seen` 防环：规格里 children 是**按 id 引用**的，引擎校验了深度和元素数，但
 * 两个元素互相引用仍然能通过 —— 那会让这里无限递归把渲染进程打挂。
 */
function Element_({ spec, id, seen }: { spec: SmithUiSpec; id: string; seen: ReadonlySet<string> }) {
  const element: SmithUiElement | undefined = spec.elements[id];
  if (!element || seen.has(id)) return null;
  const nextSeen = new Set(seen).add(id);
  const p = (element.props ?? {}) as Props;

  const children = (element.children ?? []).map((childId) => (
    <Element_ key={childId} spec={spec} id={childId} seen={nextSeen} />
  ));

  switch (element.type) {
    case "Box":
      return <div style={boxStyle(p)}>{children}</div>;
    case "Card":
      return (
        <div className="sui-card">
          {str(p.title) ? <div className="sui-card-title">{str(p.title)}</div> : null}
          {str(p.subtitle) ? <div className="sui-card-sub">{str(p.subtitle)}</div> : null}
          <div className="sui-card-body">{children}</div>
        </div>
      );
    case "Text":
      return <div className="sui-text"><Text_ p={p} />{children}</div>;
    case "Newline":
      return <div style={{ height: `${(num(p.count) ?? 1) * 12}px` }} />;
    case "Spacer":
      return <div style={{ flex: 1 }} />;
    case "Heading":
      return <Heading_ p={p} />;
    case "Divider":
      return <Divider_ p={p} />;
    case "Badge":
      return <Badge_ p={p} />;
    case "ProgressBar":
      return <ProgressBar_ p={p} />;
    case "Sparkline":
      return <Sparkline_ p={p} />;
    case "BarChart":
      return <BarChart_ p={p} />;
    case "Table":
      return <Table_ p={p} />;
    case "List":
      return <List_ p={p} />;
    case "ListItem":
      return <ListItem_ p={p} />;
    case "KeyValue":
      return <KeyValue_ p={p} />;
    case "StatusLine":
      return <StatusLine_ p={p} />;
    case "Metric":
      return <Metric_ p={p} />;
    case "Callout":
      return <Callout_ p={p} />;
    default:
      // 白名单之外的类型到不了这里（引擎会拒），但真到了就说清楚是哪个，
      // 而不是静默画一片空白。
      return <div className="sui-unknown">[未支持的组件: {String(element.type)}]</div>;
  }
}

/** 图片附件。渲染进程读不到本地文件路径（sandbox + 非 file:// 源），只回显说明。 */
function Images({ images }: Pick<SmithUiPayload, "images">) {
  if (images.length === 0) return null;
  return (
    <div className="sui-images">
      {images.map((image) => (
        // ponytail: 只回显文件名与 alt。要真正内联显示得由主进程读文件转 data URI
        // 再走 IPC 送过来 —— 等真有人附图片再做（引擎上限 4 张，常态是 0 张）。
        <div key={image.path} className="sui-image-stub">
          <span className="sui-image-name">{image.path.split("/").pop()}</span>
          {image.alt ? <span className="sui-image-alt">{image.alt}</span> : null}
        </div>
      ))}
    </div>
  );
}

export function SmithUiBlock({ payload }: { payload: SmithUiPayload }) {
  return (
    <div className="block sui-block">
      <div className="sui-tag">structured result</div>
      <Element_ spec={payload.spec} id={payload.spec.root} seen={new Set()} />
      <Images images={payload.images} />
    </div>
  );
}
