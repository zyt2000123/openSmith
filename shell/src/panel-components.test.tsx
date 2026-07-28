import assert from "node:assert/strict";
import test from "node:test";

import { renderToString, Text } from "ink";
import type { TokenDay } from "./api.js";
import { MultiSelectList } from "./multi-select-list.js";
import { PanelContainer } from "./panel-container.js";
import { RunExplorerPanel } from "./run-panel.js";
import { TabbedPanel } from "./tabbed-panel.js";
import { planModelsRow, planOverviewRow, planRollingBarChart, TokenStatsPanel } from "./token-panel.js";

function stripAnsi(text: string): string {
  const ansiEscape = String.fromCharCode(27);
  return text.replace(new RegExp(`${ansiEscape}\\[[0-?]*[ -/]*[@-~]`, "g"), "");
}

test("PanelContainer keeps the title, guidance, body, and footer together", () => {
  const output = stripAnsi(
    renderToString(
      <PanelContainer title="Skills" description="Choose a capability" footer="Esc back">
        <Text>Body</Text>
      </PanelContainer>,
    ),
  );

  assert.match(output, /Skills/);
  assert.match(output, /Choose a capability/);
  assert.match(output, /Body/);
  assert.match(output, /Esc back/);
});

test("TabbedPanel marks exactly the active tab and retains its content", () => {
  const output = stripAnsi(
    renderToString(
      <TabbedPanel
        tabs={[
          { id: "overview", label: "Overview" },
          { id: "models", label: "Models" },
        ]}
        selected="models"
        hint="←/→ switch"
      >
        <Text>Model details</Text>
      </TabbedPanel>,
    ),
  );

  assert.match(output, /Overview/);
  assert.match(output, /Models/);
  assert.match(output, /Model details/);
  assert.match(output, /←\/→ switch/);
});

test("MultiSelectList retains checked state, focus, and visible-window hints", () => {
  const output = stripAnsi(
    renderToString(
      <MultiSelectList
        items={[
          { id: "first", label: "First", description: "enabled", selected: true },
          { id: "second", label: "Second", description: "disabled", selected: false },
        ]}
        focusedIndex={3}
        startIndex={2}
        totalCount={5}
      />,
    ),
  );

  assert.match(output, /↑ more/);
  assert.match(output, /\[✓\] First/);
  assert.match(output, /> \[ \] Second/);
  assert.match(output, /↓ more/);
});

test("TokenStatsPanel composes the common panel and tab presentation without changing its loading state", () => {
  const output = stripAnsi(renderToString(<TokenStatsPanel stats={null} selectedTab="overview" />));

  assert.match(output, /Token usage/);
  assert.match(output, /Overview/);
  assert.match(output, /Models/);
  assert.match(output, /Loading local token statistics/);
});

test("RunExplorerPanel keeps the panel boundary when no run data exists yet", () => {
  const output = stripAnsi(renderToString(<RunExplorerPanel runs={[]} health={null} incidents={null} />));

  assert.match(output, /Observability/);
  assert.match(output, /No completed or interrupted runs recorded yet/);
  assert.match(output, /Esc back/);
});

function usageDay(date: string, total: number): TokenDay {
  return { date, sessions: 1, input_tokens: total, output_tokens: 0, total_tokens: total };
}

/** The shape of the PTY capture that exposed the defect: a quiet week, two heavy days. */
const ROLLING_WEEK: TokenDay[] = [
  usageDay("2026-07-19", 0),
  usageDay("2026-07-20", 0),
  usageDay("2026-07-21", 0),
  usageDay("2026-07-22", 136_900),
  usageDay("2026-07-23", 0),
  usageDay("2026-07-24", 0),
  usageDay("2026-07-25", 317_500),
];

test("a wide terminal keeps full weekday labels and one-decimal counts", () => {
  const plan = planRollingBarChart(ROLLING_WEEK, 100);

  assert.ok(plan);
  assert.equal(plan.width, 10);
  assert.equal(plan.labels[3], "Wed 22");
  assert.equal(plan.values[3], "136.9k");
});

test("a 40-column terminal falls back to day numbers and whole-unit counts", () => {
  const plan = planRollingBarChart(ROLLING_WEEK, 40);

  assert.ok(plan);
  assert.deepEqual(plan.labels, ["19", "20", "21", "22", "23", "24", "25"]);
  assert.equal(plan.values[3], "137k");
  assert.equal(plan.values[6], "318k");
});

test("no bar chart column is ever narrower than the text it holds", () => {
  // Ink wraps instead of clipping, so a column narrower than its own text used to
  // fold `136.9k` into `136.9` plus an orphan `k` on the next row at 40 columns.
  for (let columns = 8; columns <= 140; columns++) {
    const plan = planRollingBarChart(ROLLING_WEEK, columns);
    if (!plan) continue;

    for (const text of [...plan.labels, ...plan.values]) {
      assert.ok(
        text.length <= plan.width,
        `${columns} columns: ${JSON.stringify(text)} is ${text.length} wide, column is ${plan.width}`,
      );
    }
    assert.ok(
      plan.width * ROLLING_WEEK.length <= columns,
      `${columns} columns: chart spans ${plan.width * ROLLING_WEEK.length}`,
    );
  }
});

test("a terminal too narrow for even the short form drops the chart", () => {
  assert.equal(planRollingBarChart(ROLLING_WEEK, 12), null);
});

// ── Audit 2026-07-26 P2: Overview and Models must respect terminal width ──

test("overview keeps the full date and a bar on a wide terminal", () => {
  const plan = planOverviewRow(120, 7);

  assert.equal(plan.dateWidth, 10);
  assert.ok(plan.barWidth >= 4, `expected a usable bar, got ${plan.barWidth}`);
});

test("overview fits 36 columns, where the fixed 32-wide bar used to wrap", () => {
  const plan = planOverviewRow(36, 7);

  // The full date still fits here — the old bug was the hard-coded 32-column
  // bar, not the date.
  assert.equal(plan.dateWidth, 10);
  assert.ok(plan.barWidth >= 4 && plan.barWidth <= 32 - 20, `bar too wide: ${plan.barWidth}`);
});

test("overview shortens the date once the full one crowds out the bar", () => {
  const plan = planOverviewRow(26, 7);

  assert.equal(plan.dateWidth, 5, "expected MM-DD");
  assert.ok(plan.barWidth >= 4);
});

test("overview keeps only the number when nothing else fits", () => {
  const plan = planOverviewRow(14, 7);

  assert.equal(plan.dateWidth, 0);
  assert.equal(plan.barWidth, 0);
});

test("overview drops the bar rather than wrapping the number", () => {
  const plan = planOverviewRow(20, 7);

  assert.equal(plan.barWidth, 0);
});

test("overview row never exceeds the usable width", () => {
  for (const columns of [16, 20, 24, 30, 36, 48, 60, 80, 120]) {
    const valueWidth = 7;
    const plan = planOverviewRow(columns, valueWidth);
    const separators = (plan.dateWidth > 0 ? 1 : 0) + 1;
    const used = plan.dateWidth + plan.barWidth + separators + valueWidth;
    assert.ok(used <= Math.max(1, columns - 4), `columns=${columns} used=${used} budget=${columns - 4}`);
  }
});

test("models shrinks the name column before dropping the sessions suffix", () => {
  const wide = planModelsRow(120);
  assert.equal(wide.nameWidth, 28);
  assert.equal(wide.showSessions, true);

  const narrow = planModelsRow(48);
  assert.ok(narrow.nameWidth < 28, "expected a narrower name column");
  assert.equal(narrow.showSessions, true);
});

test("models drops the sessions suffix on a very narrow terminal", () => {
  const plan = planModelsRow(28);

  assert.equal(plan.showSessions, false);
  assert.ok(plan.nameWidth >= 4);
});

test("models row never exceeds the usable width", () => {
  for (const columns of [16, 24, 28, 36, 48, 60, 80, 120]) {
    const plan = planModelsRow(columns);
    const used = plan.nameWidth + 8 + (plan.showSessions ? 16 : 0);
    assert.ok(used <= Math.max(1, columns - 4), `columns=${columns} used=${used}`);
  }
});
