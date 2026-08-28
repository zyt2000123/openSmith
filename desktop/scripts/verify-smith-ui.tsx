/**
 * SmithUi.tsx 的渲染自检。
 *
 * 跑: npm run verify:smith-ui
 *
 * 为什么需要它: `parseSmithUiPayload()` 只校验结构, **props 原样透传不做
 * 校验**。实测模型发出的 props 与 `@json-render/ink` catalog 的词汇对不上
 * (ProgressBar 发 value 而非 progress、KeyValue 发 entries[] 而非
 * label/value、StatusLine 发 label+detail 而非 text、BarChart 发
 * labels[]+values[] 而非 data[])。渲染器为此做了宽容读取, 那几条别名分支
 * 没有类型能保护 —— 只能靠这里钉住。两套词汇都断言。
 *
 * 不引入测试框架: 用的是本就存在的 esbuild + react-dom/server。
 */

import { renderToStaticMarkup } from "react-dom/server";

import { SmithUiBlock } from "../src/renderer/SmithUi.tsx";

// 用户截图里那份真实载荷 —— 用的正是与 catalog 对不上的那套 props 名。
const payload = {
  version: 1 as const,
  images: [],
  spec: {
    root: "demo",
    elements: {
      demo:      { type: "Card",        props: { title: "Smith 能力总览", subtitle: "当前会话演示" }, children: ["heading","summary","progress1","status","chart","list","metric","callout","table"] },
      heading:   { type: "Heading",     props: { level: 3, text: "组件展示" }, children: [] },
      summary:   { type: "KeyValue",    props: { entries: [{ key: "Agent", value: "Smith (GLM-5.2)" }, { key: "供应商", value: "sophnet" }] }, children: [] },
      progress1: { type: "ProgressBar", props: { color: "green", label: "引擎核心模块", value: 0.92 }, children: [] },
      status:    { type: "StatusLine",  props: { detail: "所有核心模块运行正常", label: "引擎状态", status: "success" }, children: [] },
      chart:     { type: "BarChart",    props: { labels: ["read_file","shell"], title: "工具使用频率", unit: "次", values: [45, 32] }, children: [] },
      // catalog 词汇的那套也必须仍然работ —— 两套都接
      list:      { type: "List",        props: { items: ["安装依赖","跑测试"], ordered: true }, children: [] },
      metric:    { type: "Metric",      props: { label: "Price", value: "$70,686", detail: "24h", trend: "up" }, children: [] },
      callout:   { type: "Callout",     props: { type: "tip", title: "要点", content: "宽容读取两套 props" }, children: [] },
      table:     { type: "Table",       props: { columns: [{ header:"Name", key:"name" }], rows: [{ name: "api-server" }] }, children: [] },
    },
  },
} as never;

const html = renderToStaticMarkup(<SmithUiBlock payload={payload} />);

const checks: [string, boolean][] = [
  ["Card title",            html.includes("Smith 能力总览")],
  ["Card subtitle",         html.includes("当前会话演示")],
  ["Heading level=3(数字)", html.includes("sui-h3") && html.includes("组件展示")],
  ["KeyValue entries[]",    html.includes("Agent") && html.includes("Smith (GLM-5.2)") && html.includes("sophnet")],
  ["ProgressBar value->92%",html.includes("92%") && html.includes("width:92.0%")],
  ["StatusLine label+detail", html.includes("引擎状态") && html.includes("所有核心模块运行正常") && html.includes("✔")],
  ["BarChart labels/values", html.includes("read_file") && html.includes("45次") && html.includes("工具使用频率")],
  ["List ordered",          html.includes("<ol") && html.includes("安装依赖")],
  ["Metric trend up",       html.includes("$70,686") && html.includes("▲")],
  ["Callout tip",           html.includes("要点") && html.includes("宽容读取两套 props")],
  ["Table",                 html.includes("api-server")],
  ["无原始 JSON 残留",       !html.includes('"props"')],
];

let bad = 0;
for (const [name, ok] of checks) {
  if (!ok) bad++;
  console.log(`${ok ? "  OK  " : " FAIL "}${name}`);
}
console.log(bad === 0 ? "\n全部通过" : `\n${bad} 项失败`);
process.exit(bad === 0 ? 0 : 1);
