import { Box, Text } from "ink";

import type { AgentHealth, ObservabilityRun, RunIncident } from "./api.js";
import { PanelContainer } from "./panel-container.js";
import { ERROR, INFO, MUTED, WARNING } from "./theme.js";
import { formatTokenCount } from "./token-stats.js";

function outcomeColor(outcome: string | null | undefined): string {
  if (outcome === "completed") return INFO;
  if (outcome === "failed") return ERROR;
  if (outcome === "incomplete" || outcome === "cancelled") return WARNING;
  return MUTED;
}

function finiteDisplayNumber(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function RunExplorerContent({
  runs,
  health,
  incidents,
}: {
  runs: ObservabilityRun[];
  health: AgentHealth | null;
  incidents: RunIncident[] | null;
}) {
  return (
    <>
      {health ? (
        <Box marginTop={1} gap={2}>
          <Text color={health.success_rate >= 0.8 ? INFO : WARNING}>
            success {(health.success_rate * 100).toFixed(0)}% ({health.completed_count}/{health.run_count})
          </Text>
          <Text color={MUTED}>{formatTokenCount(health.tokens_per_run)} tokens/run</Text>
          <Text color={MUTED}>{finiteDisplayNumber(health.average_backtracks).toFixed(1)} backtracks/run</Text>
        </Box>
      ) : (
        <Text color={WARNING}>Health summary is unavailable.</Text>
      )}
      {incidents === null ? (
        <Box marginTop={1}>
          <Text color={WARNING}>Incident summary is unavailable.</Text>
        </Box>
      ) : incidents.length ? (
        <Box flexDirection="column" marginTop={1}>
          <Text color={WARNING}>Incidents</Text>
          {incidents.slice(0, 3).map((incident) => (
            <Text
              key={`${incident.run_id}:${incident.category}`}
              color={incident.severity === "error" ? ERROR : WARNING}
            >
              {incident.severity.toUpperCase()} · {incident.category} · {incident.run_id.slice(0, 10)} ·{" "}
              {incident.message}
            </Text>
          ))}
        </Box>
      ) : (
        <Box marginTop={1}>
          <Text color={INFO}>No active incident in the displayed window.</Text>
        </Box>
      )}
      <Box marginTop={1}>
        <Text color={MUTED}>Recent runs</Text>
      </Box>
      {runs.map((run) => (
        <Box key={run.run_id} flexDirection="column" marginTop={1}>
          <Box gap={1}>
            <Text color={outcomeColor(run.outcome)} bold>
              {run.outcome || "unknown"}
            </Text>
            <Text color={MUTED}>{(run.finished_at ?? "").replace("T", " ").slice(0, 19)}</Text>
            <Text color={MUTED}>{run.run_id.slice(0, 10)}</Text>
          </Box>
          <Text color={MUTED}>
            {run.event_count} events · {run.tool_call_count} tools · {run.backtrack_count} backtracks ·{" "}
            {formatTokenCount(run.total_tokens)} tokens
            {run.forced_skill ? ` · skill ${run.forced_skill}` : ""}
            {run.reason ? ` · ${run.reason}` : ""}
          </Text>
        </Box>
      ))}
    </>
  );
}

export function RunExplorerPanel({
  runs,
  health,
  incidents,
}: {
  runs: ObservabilityRun[] | null;
  health: AgentHealth | null;
  incidents: RunIncident[] | null;
}) {
  const content =
    runs === null ? (
      <Text color={MUTED}>Loading observability…</Text>
    ) : runs.length === 0 ? (
      <Text color={MUTED}>No completed or interrupted runs recorded yet.</Text>
    ) : (
      <RunExplorerContent runs={runs} health={health} incidents={incidents} />
    );

  return (
    <PanelContainer title="Observability" description="Local run health · `/trace <run-id>` for RCA" footer="Esc back">
      {content}
    </PanelContainer>
  );
}
