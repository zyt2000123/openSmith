import { createRequire } from "node:module";
import path from "node:path";
import { useShikiHighlighter } from "@assistant-ui/react-ink-markdown";
import { Box, render, Static, Text, useApp } from "ink";
import { InkPictureProvider } from "ink-picture";
import Spinner from "ink-spinner";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStore } from "zustand";

import type { PendingApproval, SkillSummary } from "./api.js";
import { approvalDetails, approvalReason, approvalSummary, approvalTitle } from "./approval.js";
import { NodeBridge } from "./bridge.js";
import type { CodeHighlighter } from "./code-block.js";
import {
  acceptsComposerSubmission,
  buildSlashItems,
  filterSlash,
  parseSkill,
  runShellCommand,
  type SlashItem,
  selectedSlashItem,
} from "./commands.js";
import { Composer } from "./composer.js";
import { loadHistory, saveHistory } from "./history.js";
import { LIFECYCLE_HOOKS } from "./hooks.js";
import { RunProgress, StatusHud } from "./hud.js";
import { exitHistoryBrowsing, useShellInput } from "./input.js";
import {
  getCategorizedVisibleList,
  getVisibleList,
  SKILLS_PANEL_VISIBLE_ITEMS,
  SLASH_MENU_VISIBLE_ITEMS,
} from "./list-navigation.js";
import {
  MODEL_PICKER_VISIBLE_ITEMS,
  type ModelPickerState,
  modelPickerOptions,
  modelPickerTargetLabel,
} from "./model-picker.js";
import { MultiSelectList } from "./multi-select-list.js";
import { PanelContainer } from "./panel-container.js";
import type { QueuedMessage } from "./queue.js";
import { RunExplorerPanel } from "./run-panel.js";
import {
  buildLlmConfigInput,
  fieldValue,
  hasStoredApiKey,
  isApiKeySetupField,
  nextSetupIndex,
  setProvider,
  setSetupField,
  setupFieldAt,
  setupFieldLabel,
  setupFields,
} from "./setup.js";
import {
  filterSkillMentions,
  filterSkills,
  isSkillEnabled,
  isSkillMentionQuery,
  parseSkillMention,
  selectedSkillMentionState,
} from "./skill-mention.js";
import { type AppStore, createAppStore } from "./store.js";
import { clearTerminal } from "./term.js";
import { truncateDisplay } from "./text-layout.js";
import { ACCENT, BORDER, ERROR, INFO, MUTED, SELECTED_BACKGROUND, SELECTED_FOREGROUND, WARNING } from "./theme.js";
import { TokenStatsPanel } from "./token-panel.js";
import { TranscriptEntryView } from "./transcript.js";
import { splitTranscript, type TranscriptEntry, type TranscriptViewMode } from "./transcript-state.js";
import { useWindowSize } from "./window-size.js";

const SHELL_VERSION = (createRequire(import.meta.url)("../package.json") as { version: string }).version;
const PROJECT_CWD = path.resolve(process.env.SMITH_PROJECT_CWD?.trim() || process.cwd());

const SMITH_LOGO = [
  "███████╗███╗   ███╗██╗████████╗██╗  ██╗",
  "██╔════╝████╗ ████║██║╚══██╔══╝██║  ██║",
  "███████╗██╔████╔██║██║   ██║   ███████║",
  "╚════██║██║╚██╔╝██║██║   ██║   ██╔══██║",
  "███████║██║ ╚═╝ ██║██║   ██║   ██║  ██║",
  "╚══════╝╚═╝     ╚═╝╚═╝   ╚═╝   ╚═╝  ╚═╝",
];
const GHOST_BUDDY = ["  ─╥╥─  ", "▄██████▄", "██ ██ ██", " ██████ ", "╰╯╰╮╭╯╰╯"];

const store = createAppStore(loadHistory());
const bridge = new NodeBridge(store);
const getState = store.getState;

/** Items rendered once through <Static>: the hero banner plus every completed entry. */
type StaticItem = { kind: "hero"; id: string } | TranscriptEntry;

function useS<T>(selector: (state: AppStore) => T): T {
  return useStore(store, selector);
}

function truncate(text: string, max = 80): string {
  return truncateDisplay(text, max);
}

function armSkill(skill: SkillSummary): void {
  getState().set({ pendingSkill: skill, panel: "chat", statusLine: "" });
}

const HERO_HINTS = ["`/` for commands", "`@` for skills", "Enter confirms", "Esc goes back", "`/help` for all"];

/** border(2) + paddingX(6) + logo(39) + gap(3) + buddy(8) — below this the art itself will not fit. */
const HERO_MIN_COLUMNS = 58;
/** The above + gap(5) + widest tip(18): only then do the two columns sit side by side. */
const HERO_WIDE_COLUMNS = 82;

function HeroPanel() {
  const { columns } = useWindowSize();
  const compact = columns < HERO_MIN_COLUMNS;
  const wide = columns >= HERO_WIDE_COLUMNS;

  if (compact) {
    return (
      <Box flexDirection="column" marginBottom={1} paddingTop={1}>
        <Box gap={1} marginBottom={1}>
          <Text color={ACCENT}>Agent-Smith</Text>
          <Text color={MUTED}>v{SHELL_VERSION}</Text>
        </Box>
        <Text color={INFO}>Terminal view is compact. Type `/help` for commands.</Text>
      </Box>
    );
  }

  return (
    <Box borderColor={ACCENT} borderStyle="round" flexDirection="column" marginBottom={1} paddingX={3} paddingY={1}>
      {/* Two columns, each headed by its own title row, so "Tips:" lands on the
          same line as the version without either side needing a fixed width. */}
      <Box flexDirection={wide ? "row" : "column"} gap={wide ? 5 : 1}>
        <Box flexDirection="column">
          <Box gap={1}>
            <Text color={ACCENT}>Agent-Smith</Text>
            <Text color={MUTED}>v{SHELL_VERSION}</Text>
          </Box>
          <Box gap={3} marginTop={1}>
            <Box flexDirection="column">
              {SMITH_LOGO.map((line) => (
                <Text color={ACCENT} key={line}>
                  {line}
                </Text>
              ))}
            </Box>
            <Box flexDirection="column" justifyContent="center">
              {GHOST_BUDDY.map((line) => (
                <Text color={ACCENT} key={line}>
                  {line}
                </Text>
              ))}
            </Box>
          </Box>
        </Box>
        <Box flexDirection="column">
          <Text bold color={ACCENT}>
            Tips:
          </Text>
          <Box flexDirection="column" marginTop={1}>
            {HERO_HINTS.map((hint) => (
              <Text color={INFO} key={hint}>
                {"· "}
                {hint}
              </Text>
            ))}
          </Box>
        </Box>
      </Box>
    </Box>
  );
}

function SetupPanel() {
  const config = useS((state) => state.config);
  const draft = useS((state) => state.setupDraft);
  const flow = useS((state) => state.setupFlow);
  const index = useS((state) => state.setupIndex);
  const fields = setupFields(flow);
  const activeField = setupFieldAt(index, flow);

  return (
    <PanelContainer
      title={flow === "initial" ? "SMITH SETUP" : "LLM CONFIG"}
      description={
        flow === "initial"
          ? "Connect a model to get started."
          : "Tab/arrows to move. Advanced route and timeout fields are optional."
      }
    >
      <Box flexDirection="column" marginTop={1}>
        {fields.map((field) => (
          <Text key={field} color={field === activeField ? ACCENT : INFO}>
            {field === activeField ? ">" : " "}{" "}
            {isApiKeySetupField(field) && hasStoredApiKey(config, field)
              ? `${setupFieldLabel(field)} (blank=keep)`
              : setupFieldLabel(field)}
            :{" "}
            {isApiKeySetupField(field)
              ? "•".repeat(fieldValue(draft, field).length) || "-"
              : truncate(fieldValue(draft, field), 104) || "-"}
          </Text>
        ))}
      </Box>
    </PanelContainer>
  );
}

function SessionsPanel() {
  const sessions = useS((state) => state.sessions);
  return (
    <PanelContainer title="Recent sessions">
      {sessions.length === 0 ? (
        <Text color={MUTED}>No sessions.</Text>
      ) : (
        sessions.slice(0, 8).map((session) => (
          <Text key={session.id} color={INFO}>
            {session.id} {truncate(session.title, 40)} {session.message_count} msg
          </Text>
        ))
      )}
    </PanelContainer>
  );
}

function SkillsPanel() {
  const skills = useS((state) => state.skills);
  const selectedIndex = useS((state) => state.skillsIndex);
  const enabledSkills = skills.filter(isSkillEnabled);
  const visible = getVisibleList(enabledSkills, selectedIndex, SKILLS_PANEL_VISIBLE_ITEMS);
  return (
    <PanelContainer title="Enabled skills" footer="↑/↓ select · Enter run · Esc back">
      {enabledSkills.length === 0 ? (
        <Text color={MUTED}>No enabled skills.</Text>
      ) : (
        <>
          {visible.startIndex > 0 ? <Text color={MUTED}>↑ more skills</Text> : null}
          {visible.items.map((skill, offset) => {
            const index = visible.startIndex + offset;
            return (
              <Box key={skill.name} flexDirection="column" marginBottom={1}>
                <Text color={index === selectedIndex ? ACCENT : INFO}>
                  {index === selectedIndex ? ">" : " "} {skill.name} <Text color={MUTED}>[{skill.source}]</Text>
                </Text>
                <Text color={MUTED}>{skill.description || ""}</Text>
              </Box>
            );
          })}
          {visible.startIndex + visible.items.length < enabledSkills.length ? (
            <Text color={MUTED}>↓ more skills</Text>
          ) : null}
        </>
      )}
    </PanelContainer>
  );
}

function SkillActionsPanel() {
  const selectedIndex = useS((state) => state.skillActionIndex);
  const actions = [
    { title: "1. List skills", description: "Tip: press @ to open enabled skills directly." },
    { title: "2. Enable/Disable skills", description: "Turn skills on or off." },
  ];
  return (
    <PanelContainer title="Skills" description="Choose an action" footer="↑/↓ select · Enter confirm · Esc back">
      <Box flexDirection="column" marginTop={1}>
        {actions.map((action, index) => {
          const selected = index === selectedIndex;
          return (
            <Box key={action.title} width="100%" backgroundColor={selected ? SELECTED_BACKGROUND : undefined}>
              <Text color={selected ? SELECTED_FOREGROUND : INFO} bold={selected}>
                {selected ? ">" : " "} {action.title}
              </Text>
              <Text color={selected ? SELECTED_FOREGROUND : MUTED}>{`  ${action.description}`}</Text>
            </Box>
          );
        })}
      </Box>
    </PanelContainer>
  );
}

function SkillTogglePanel() {
  const skills = useS((state) => state.skills);
  const inputValue = useS((state) => state.inputValue);
  const selectedIndex = useS((state) => state.skillsIndex);
  const matchedSkills = filterSkills(skills, inputValue);
  const visible = getVisibleList(matchedSkills, selectedIndex, SKILLS_PANEL_VISIBLE_ITEMS);
  return (
    <PanelContainer
      title="Enable/Disable Skills"
      description="Turn skills on or off. Changes are saved automatically."
      footer="↑/↓ select · Enter toggle · Esc back"
    >
      <Text color={MUTED}>Type below to search skills.</Text>
      <MultiSelectList
        items={visible.items.map((skill) => ({
          id: skill.name,
          label: skill.name,
          description: truncate(skill.description, 60),
          selected: isSkillEnabled(skill),
        }))}
        focusedIndex={selectedIndex}
        startIndex={visible.startIndex}
        totalCount={matchedSkills.length}
        moreLabel="more skills"
        emptyLabel="No matching skills."
      />
    </PanelContainer>
  );
}

function McpPanel() {
  const servers = useS((state) => state.mcpServers);
  return (
    <PanelContainer title="MCP servers">
      {servers.length === 0 ? (
        <Text color={MUTED}>No MCP servers configured.</Text>
      ) : (
        servers.map((server) => (
          <Box key={server.name} flexDirection="column" marginBottom={1}>
            <Text color={server.status === "connected" ? INFO : ERROR}>
              {server.status === "connected" ? "●" : "○"} {server.name} [{server.type}] · {server.tools.length} tool(s)
            </Text>
            {server.error ? <Text color={ERROR}>{truncate(server.error, 100)}</Text> : null}
            {server.tools.slice(0, 8).map((tool) => (
              <Text key={tool.name} color={MUTED}>
                {`  ${tool.name} — ${truncate(tool.description || "No description", 90)}`}
              </Text>
            ))}
          </Box>
        ))
      )}
    </PanelContainer>
  );
}

function HooksPanel() {
  const selectedIndex = useS((state) => state.hooksIndex);
  return (
    <Box flexDirection="column">
      <Text color={ACCENT} bold>
        Hooks
      </Text>
      <Text color={MUTED}>Built-in lifecycle hooks. Handlers register on demand for each runtime.</Text>
      <Box marginTop={1}>
        <Box width={35}>
          <Text color={INFO}>Event</Text>
        </Box>
        <Box width={24}>
          <Text color={INFO}>Handler</Text>
        </Box>
        <Text color={INFO}>Description</Text>
      </Box>
      {LIFECYCLE_HOOKS.map((hook, index) => {
        const selected = index === selectedIndex;
        return (
          <Box key={hook.event} width="100%" backgroundColor={selected ? SELECTED_BACKGROUND : undefined}>
            <Box width={35}>
              <Text color={selected ? SELECTED_FOREGROUND : INFO} bold={selected}>
                {selected ? "> " : "  "}
                {hook.event}
              </Text>
            </Box>
            <Box width={24}>
              <Text color={selected ? SELECTED_FOREGROUND : MUTED}>{hook.handler}</Text>
            </Box>
            <Text color={selected ? SELECTED_FOREGROUND : MUTED}>{hook.description}</Text>
          </Box>
        );
      })}
      <Text color={MUTED}>Built-in dispatch points; configurable or plugin hooks are not loaded yet.</Text>
      <Text color={MUTED}>↑/↓ select · Enter view details · Esc close</Text>
    </Box>
  );
}

function HookDetailsPanel() {
  const selectedIndex = useS((state) => state.hooksIndex);
  const hook = LIFECYCLE_HOOKS[selectedIndex] ?? LIFECYCLE_HOOKS[0];
  if (!hook) return null;
  return (
    <Box flexDirection="column">
      <Text color={ACCENT} bold>
        {hook.event}
      </Text>
      <Text color={MUTED}>{hook.description}</Text>
      <Box marginTop={1} flexDirection="column">
        <Text color={INFO}>
          Handler <Text color={MUTED}>{hook.handler}</Text>
        </Text>
        <Text color={INFO}>
          Status <Text color={MUTED}>Enabled on dispatch</Text>
        </Text>
        <Text color={INFO}>
          Details <Text color={MUTED}>{hook.detail}</Text>
        </Text>
      </Box>
      <Text color={MUTED}>Esc back</Text>
    </Box>
  );
}

function SlashMenuRow({
  item,
  index,
  showCategory,
  selectedIndex,
}: {
  item: SlashItem;
  index: number;
  showCategory: boolean;
  selectedIndex: number;
}) {
  const selected = index === selectedIndex;
  return (
    <Box flexDirection="column" marginTop={showCategory ? 1 : 0}>
      {showCategory ? <Text color={MUTED}>{item.category}</Text> : null}
      <Box width="100%" backgroundColor={selected ? SELECTED_BACKGROUND : undefined}>
        <Text color={selected ? SELECTED_FOREGROUND : INFO} bold={selected}>
          {selected ? ">" : " "} {item.command}
        </Text>
        <Text color={selected ? SELECTED_FOREGROUND : MUTED}>{`  ${truncate(item.description, 60)}`}</Text>
      </Box>
    </Box>
  );
}

function SlashMenu({ items, selectedIndex }: { items: SlashItem[]; selectedIndex: number }) {
  const visible = getVisibleList(items, selectedIndex, SLASH_MENU_VISIBLE_ITEMS);
  const rows = getCategorizedVisibleList(visible.items, visible.startIndex);
  return (
    <Box flexDirection="column" marginBottom={1} marginTop={1}>
      <Text color={ACCENT}>Slash palette</Text>
      {items.length === 0 ? (
        <Text color={MUTED}>No matches.</Text>
      ) : (
        <>
          {visible.startIndex > 0 ? <Text color={MUTED}>↑ more</Text> : null}
          {rows.map((row) => (
            <SlashMenuRow
              key={row.item.id}
              item={row.item}
              index={row.index}
              showCategory={row.showCategory}
              selectedIndex={selectedIndex}
            />
          ))}
          {visible.startIndex + visible.items.length < items.length ? <Text color={MUTED}>↓ more</Text> : null}
        </>
      )}
    </Box>
  );
}

function SkillMentionMenu({ items, selectedIndex }: { items: SkillSummary[]; selectedIndex: number }) {
  const visible = getVisibleList(items, selectedIndex, SLASH_MENU_VISIBLE_ITEMS);
  return (
    <Box flexDirection="column" marginBottom={1} marginTop={1}>
      <Text color={ACCENT}>Skill picker</Text>
      {items.length === 0 ? (
        <Text color={MUTED}>No matching skills.</Text>
      ) : (
        <>
          {visible.startIndex > 0 ? <Text color={MUTED}>↑ more skills</Text> : null}
          {visible.items.map((skill, offset) => {
            const index = visible.startIndex + offset;
            const selected = index === selectedIndex;
            return (
              <Box key={skill.name} width="100%" backgroundColor={selected ? SELECTED_BACKGROUND : undefined}>
                <Text color={selected ? SELECTED_FOREGROUND : INFO} bold={selected}>
                  {selected ? ">" : " "} @{skill.name}
                </Text>
                <Text color={selected ? SELECTED_FOREGROUND : MUTED}>{`  ${truncate(skill.description, 60)}`}</Text>
              </Box>
            );
          })}
          {visible.startIndex + visible.items.length < items.length ? <Text color={MUTED}>↓ more skills</Text> : null}
          <Text color={MUTED}>↑/↓ select · Enter insert · Esc cancel</Text>
        </>
      )}
    </Box>
  );
}

function ShellContent({
  mode,
  panel,
  active,
  viewMode,
  welcomeNotice,
  highlighter,
  tokenStats,
  observabilityRuns,
  observabilityHealth,
  observabilityIncidents,
  tokenTab,
}: Pick<
  AppStore,
  | "mode"
  | "panel"
  | "viewMode"
  | "welcomeNotice"
  | "tokenStats"
  | "tokenTab"
  | "observabilityRuns"
  | "observabilityHealth"
  | "observabilityIncidents"
> & {
  active: TranscriptEntry[];
  highlighter?: CodeHighlighter;
}) {
  if (mode === "boot") {
    return (
      <Box>
        <Spinner type="dots" />
        <Text color={MUTED}> Booting…</Text>
      </Box>
    );
  }

  if (mode === "setup") {
    return <SetupPanel />;
  }

  if (panel === "runs") {
    return (
      <RunExplorerPanel runs={observabilityRuns} health={observabilityHealth} incidents={observabilityIncidents} />
    );
  }

  return (
    <>
      {welcomeNotice ? <Text color={welcomeNotice.tone === "error" ? ERROR : MUTED}>{welcomeNotice.text}</Text> : null}
      {panel === "skill-actions" ? <SkillActionsPanel /> : null}
      {panel === "skills" ? <SkillsPanel /> : null}
      {panel === "skill-toggle" ? <SkillTogglePanel /> : null}
      {panel === "mcp" ? <McpPanel /> : null}
      {panel === "hooks" ? <HooksPanel /> : null}
      {panel === "hook-details" ? <HookDetailsPanel /> : null}
      {panel === "sessions" ? <SessionsPanel /> : null}
      {panel === "tokens" ? <TokenStatsPanel stats={tokenStats} selectedTab={tokenTab} /> : null}
      {panel === "tokens"
        ? null
        : active.map((entry) => (
            <TranscriptEntryView key={entry.id} entry={entry} viewMode={viewMode} highlighter={highlighter} />
          ))}
    </>
  );
}

type ShellFooterProps = {
  mode: AppStore["mode"];
  panel: AppStore["panel"];
  busy: boolean;
  compressing: boolean;
  inputLocked: boolean;
  statusLine: string;
  pendingApproval: PendingApproval | null;
  approvalIndex: number;
  approvalResolving: boolean;
  modelPicker: ModelPickerState | null;
  pendingSkill: SkillSummary | null;
  queuedMessages: QueuedMessage[];
  inputValue: string;
  activeSetupField: ReturnType<typeof setupFieldAt>;
  slashMenuOpen: boolean;
  slashItems: SlashItem[];
  slashIndex: number;
  skillMentionMenuOpen: boolean;
  skillMentions: SkillSummary[];
  skillMentionIndex: number;
  viewMode: TranscriptViewMode;
  baseUrl: AppStore["baseUrl"];
  config: AppStore["config"];
  selectedModelProfile: AppStore["selectedModelProfile"];
  currentSession: AppStore["currentSession"];
  runStartedAt: AppStore["runStartedAt"];
  turnTokenUsage: AppStore["turnTokenUsage"];
  tokenUsage: AppStore["tokenUsage"];
  contextUsage: AppStore["contextUsage"];
  turnCount: number;
  toolActivity: AppStore["toolActivity"];
  onInputChange: (value: string) => void;
  onChatSubmit: (value: string) => void;
  onSetupSubmit: (value: string) => void;
};

const COMPRESSION_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"] as const;

function CompressionIndicator() {
  const [spinnerIndex, setSpinnerIndex] = useState(0);
  const [textBright, setTextBright] = useState(true);

  useEffect(() => {
    const timer = setInterval(() => {
      setSpinnerIndex((current) => (current + 1) % COMPRESSION_SPINNER_FRAMES.length);
    }, 90);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    const timer = setInterval(() => setTextBright((current) => !current), 360);
    return () => clearInterval(timer);
  }, []);

  return (
    <Box>
      <Text color={WARNING} dimColor={!textBright}>
        Automatically compressing context
      </Text>
      <Text color={WARNING}> {COMPRESSION_SPINNER_FRAMES[spinnerIndex]}</Text>
    </Box>
  );
}

function ApprovalPrompt({
  approval,
  selectedIndex,
  resolving,
}: {
  approval: PendingApproval;
  selectedIndex: number;
  resolving: boolean;
}) {
  const details = approvalDetails(approval);
  const options = ["Allow once", "Deny"];

  return (
    <Box borderColor={WARNING} borderStyle="round" flexDirection="column" marginBottom={1} paddingX={1}>
      <Text color={WARNING} bold>
        Approval required
      </Text>
      <Text color={INFO} bold>
        {approvalTitle(approval)}
      </Text>
      <Text color={INFO}>{approvalSummary(approval)}</Text>
      {details.map((detail) => (
        <Box key={detail.label}>
          <Text color={MUTED}>{detail.label}: </Text>
          <Text color={INFO}>{detail.value}</Text>
        </Box>
      ))}
      {approvalReason(approval) ? (
        <Box>
          <Text color={MUTED}>Reason: </Text>
          <Text color={INFO}>{approvalReason(approval)}</Text>
        </Box>
      ) : null}
      <Box flexDirection="column" marginTop={1}>
        {options.map((option, index) => (
          <Text key={option} color={selectedIndex === index ? ACCENT : INFO} bold={selectedIndex === index}>
            {selectedIndex === index ? "❯ " : "  "}
            {option}
          </Text>
        ))}
      </Box>
      <Text color={MUTED}>{resolving ? "Submitting decision…" : "↑/↓ select · Enter confirm · Esc cancel"}</Text>
    </Box>
  );
}

function ModelPickerPrompt({ picker }: { picker: ModelPickerState }) {
  const options = modelPickerOptions(picker);
  const { items, startIndex } = getVisibleList(options, picker.selectedIndex, MODEL_PICKER_VISIBLE_ITEMS);
  const title =
    picker.step === "model"
      ? "Choose a relay model"
      : picker.step === "target"
        ? `Configure ${picker.model} as…`
        : `Configure ${picker.model} as ${modelPickerTargetLabel(picker.target)}?`;

  return (
    <Box borderColor={ACCENT} borderStyle="round" flexDirection="column" marginBottom={1} paddingX={1}>
      <Text color={ACCENT} bold>
        Model configuration
      </Text>
      <Text color={INFO}>{title}</Text>
      <Box flexDirection="column" marginTop={1}>
        {items.map((option, index) => {
          const optionIndex = startIndex + index;
          return (
            <Text
              key={option}
              color={picker.selectedIndex === optionIndex ? ACCENT : INFO}
              bold={picker.selectedIndex === optionIndex}
            >
              {picker.selectedIndex === optionIndex ? "❯ " : "  "}
              {option}
            </Text>
          );
        })}
      </Box>
      <Text color={MUTED}>↑/↓ select · Enter continue · Esc cancel</Text>
    </Box>
  );
}

function footerPlaceholder(props: ShellFooterProps): string {
  if (props.mode === "setup") {
    return props.activeSetupField === "save" ? "Enter to save" : `Edit ${setupFieldLabel(props.activeSetupField)}`;
  }
  if (props.panel === "skill-actions") return "Use ↑/↓ and Enter to choose…";
  if (props.panel === "skills") return "Use ↑/↓ and Enter to run a skill…";
  if (props.panel === "skill-toggle") return "Type to search skills…";
  if (props.panel === "hooks") return "Use ↑/↓ and Enter to inspect a hook…";
  if (props.panel === "hook-details") return "Press Esc to return to hooks…";
  return props.pendingSkill ? `Ask Smith to run ${props.pendingSkill.name}…` : "Tell Smith what to do…";
}

function FooterStatus({
  compressing,
  busy,
  statusLine,
}: Pick<ShellFooterProps, "compressing" | "busy" | "statusLine">) {
  if (compressing) return <CompressionIndicator />;
  if (!busy) return <Text color={MUTED}>{statusLine}</Text>;
  return null;
}

function SkillIndicator({ skill }: { skill: SkillSummary | null }) {
  if (!skill) return null;
  return (
    <Box marginBottom={1}>
      <Text color={ACCENT}>Skill </Text>
      <Text color={ACCENT} bold>
        {skill.name}
      </Text>
      <Text color={MUTED}> armed — Esc to clear</Text>
    </Box>
  );
}

function FooterInput(props: ShellFooterProps) {
  if (props.modelPicker) return <ModelPickerPrompt picker={props.modelPicker} />;
  if (props.pendingApproval) {
    return (
      <ApprovalPrompt
        approval={props.pendingApproval}
        selectedIndex={props.approvalIndex}
        resolving={props.approvalResolving}
      />
    );
  }

  const placeholder = footerPlaceholder(props);
  return (
    <>
      <Box borderColor={props.busy ? ACCENT : BORDER} borderStyle="round" paddingX={1}>
        <Text color={ACCENT}>{"❯ "}</Text>
        <Composer
          value={props.inputValue}
          placeholder={placeholder}
          focus={!props.compressing && !props.inputLocked}
          showCursor={!props.compressing && !props.inputLocked}
          mask={props.mode === "setup" && isApiKeySetupField(props.activeSetupField) ? "•" : undefined}
          onChange={props.onInputChange}
          onSubmit={props.mode === "setup" ? props.onSetupSubmit : props.onChatSubmit}
        />
      </Box>
      {props.slashMenuOpen ? <SlashMenu items={props.slashItems} selectedIndex={props.slashIndex} /> : null}
      {props.skillMentionMenuOpen ? (
        <SkillMentionMenu items={props.skillMentions} selectedIndex={props.skillMentionIndex} />
      ) : null}
    </>
  );
}

function ShellFooter(props: ShellFooterProps) {
  if (props.mode === "boot") return null;

  return (
    <>
      <FooterStatus compressing={props.compressing} busy={props.busy} statusLine={props.statusLine} />
      <SkillIndicator skill={props.pendingSkill} />
      <QueuePreview items={props.queuedMessages} />
      {props.busy && props.runStartedAt !== null ? (
        <RunProgress startedAt={props.runStartedAt} tokenUsage={props.turnTokenUsage} />
      ) : null}
      <FooterInput {...props} />
      <StatusHud
        model={props.selectedModelProfile || props.config?.model || "-"}
        projectName={path.basename(PROJECT_CWD)}
        cwd={PROJECT_CWD}
        baseUrl={props.baseUrl}
        sessionId={props.currentSession?.id}
        tokenUsage={props.tokenUsage}
        contextUsage={props.contextUsage}
        toolActivity={props.toolActivity}
        turnCount={props.turnCount}
        viewMode={props.viewMode}
      />
    </>
  );
}

function completeSlashSelection(input: string, slashMenuOpen: boolean, items: SlashItem[], index: number): boolean {
  const selected = selectedSlashItem(input, slashMenuOpen, items, index);
  if (!selected) return false;

  getState().set({ inputValue: selected.command, statusLine: `Ready: ${selected.command}` });
  return true;
}

function completeSkillMentionSelection(skillMentionMenuOpen: boolean, items: SkillSummary[], index: number): boolean {
  if (!skillMentionMenuOpen) return false;

  const selected = items[index];
  if (!selected) return false;

  getState().set(selectedSkillMentionState(selected));
  return true;
}

function QueuePreview({ items }: { items: QueuedMessage[] }) {
  if (items.length === 0) return null;

  return (
    <Box flexDirection="column" marginBottom={1} paddingX={1}>
      <Box>
        <Text color={MUTED}>• </Text>
        <Text color={INFO}>Queued follow-up inputs</Text>
      </Box>
      {items.map((item) => (
        <Box key={item.id}>
          <Text color={MUTED}>{"  ↳ "}</Text>
          <Text color={MUTED}>{truncate(item.text, 120)}</Text>
        </Box>
      ))}
      <Text color={MUTED}>{"    shift + ← edit last queued message"}</Text>
    </Box>
  );
}

function rememberSubmittedInput(input: string): void {
  const state = getState();
  state.pushHistory(input);
  saveHistory(getState().inputHistory);
  state.set({ inputValue: "" });
}

async function submitWhileBusy(
  input: string,
  payload: string,
  skill: SkillSummary | null,
  slashMenuOpen: boolean,
  slashItems: SlashItem[],
  slashIndex: number,
): Promise<void> {
  if (input.startsWith("/")) {
    if (!completeSlashSelection(input, slashMenuOpen, slashItems, slashIndex)) {
      getState().set({ statusLine: `${input} is unavailable while Smith is working. Press Esc to cancel the run.` });
    }
    return;
  }
  if (!bridge.enqueueMessage(payload, skill?.name)) return;

  rememberSubmittedInput(input);
  if (skill) getState().set({ pendingSkill: null });
}

async function submitSlashCommand(
  input: string,
  hasExplicitSkill: boolean,
  slashMenuOpen: boolean,
  slashItems: SlashItem[],
  slashIndex: number,
  exit: () => void,
): Promise<boolean> {
  if (!input.startsWith("/") || hasExplicitSkill) return false;

  // A highlighted palette entry wins over the raw text so a partial command still runs.
  const selected = selectedSlashItem(input, slashMenuOpen, slashItems, slashIndex);
  rememberSubmittedInput(input);
  await runShellCommand(selected?.command ?? input, { bridge, exit, getState, workingDir: PROJECT_CWD });
  return true;
}

async function submitChat(
  value: string,
  panel: AppStore["panel"],
  busy: boolean,
  pendingSkill: SkillSummary | null,
  skills: SkillSummary[],
  slashMenuOpen: boolean,
  slashItems: SlashItem[],
  slashIndex: number,
  skillMentionMenuOpen: boolean,
  skillMentions: SkillSummary[],
  skillMentionIndex: number,
  exit: () => void,
): Promise<void> {
  if (!acceptsComposerSubmission(panel)) {
    // The list panels keep accepting characters, so Enter looked broken: it was
    // rejected here without a word, leaving the user to discover on their own
    // that the composer has to be cleared with Esc first.
    if (value.trim()) {
      getState().set({ statusLine: "Press Esc to leave this panel before sending a message." });
    }
    return;
  }
  const input = value.trim();
  if (!input) return;

  if (completeSkillMentionSelection(skillMentionMenuOpen, skillMentions, skillMentionIndex)) return;

  const explicitSkill = parseSkillMention(input, skills) ?? parseSkill(input, skills);
  const skill = explicitSkill?.skill ?? pendingSkill;
  const payload = explicitSkill?.prompt || input;

  if (explicitSkill && !explicitSkill.prompt) {
    rememberSubmittedInput(input);
    armSkill(explicitSkill.skill);
    return;
  }

  if (busy) {
    await submitWhileBusy(input, payload, skill, slashMenuOpen, slashItems, slashIndex);
    return;
  }

  if (await submitSlashCommand(input, Boolean(explicitSkill), slashMenuOpen, slashItems, slashIndex, exit)) return;

  rememberSubmittedInput(input);
  if (skill) getState().set({ pendingSkill: null });
  const accepted = await bridge.sendMessage(payload, skill?.name);
  if (!accepted) getState().set({ inputValue: input });
}

async function submitSetup(
  value: string,
  activeField: ReturnType<typeof setupFieldAt>,
  configHasApiKey: boolean,
  setupIndex: number,
  setupFlow: AppStore["setupFlow"],
): Promise<void> {
  const state = getState();
  if (activeField === "save") {
    const draft = state.setupDraft;
    if (!draft.vendor.trim() || !draft.base_url.trim() || !draft.model.trim()) {
      state.set({ statusLine: "Supplier name, base URL, and model required." });
      return;
    }
    if (!draft.api_key.trim() && !configHasApiKey) {
      state.set({ statusLine: "API key required." });
      return;
    }
    try {
      await bridge.saveConfig(buildLlmConfigInput(draft));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      state.set({ statusLine: `Configuration error: ${message}` });
    }
    return;
  }

  const draft =
    activeField === "provider"
      ? setProvider(state.setupDraft, value)
      : setSetupField(state.setupDraft, activeField, value);
  if (!draft) {
    state.set({ statusLine: "Must be openai, anthropic, or gemini." });
    return;
  }

  const nextIndex = nextSetupIndex(setupIndex, 1, false, setupFlow);
  const nextField = setupFieldAt(nextIndex, setupFlow);
  state.set({
    setupDraft: draft,
    setupIndex: nextIndex,
    inputValue: nextField === "save" ? "" : fieldValue(draft, nextField),
  });
}

function SmithApp() {
  const { exit } = useApp();
  const highlighter = useShikiHighlighter({ theme: "github-dark" });

  const mode = useS((state) => state.mode);
  const panel = useS((state) => state.panel);
  const busy = useS((state) => state.busy);
  const compressing = useS((state) => state.compressing);
  const inputLocked = useS((state) => state.inputLocked);
  const inputValue = useS((state) => state.inputValue);
  const statusLine = useS((state) => state.statusLine);
  const pendingSkill = useS((state) => state.pendingSkill);
  const queuedMessages = useS((state) => state.queuedMessages);
  const viewMode = useS((state) => state.viewMode);
  const baseUrl = useS((state) => state.baseUrl);
  const transcript = useS((state) => state.transcript);
  const transcriptEpoch = useS((state) => state.transcriptEpoch);
  const turnCount = useS((state) => state.turnCount);
  const toolActivity = useS((state) => state.toolActivity);
  const turnTokenUsage = useS((state) => state.turnTokenUsage);
  const tokenUsage = useS((state) => state.tokenUsage);
  const contextUsage = useS((state) => state.contextUsage);
  const tokenStats = useS((state) => state.tokenStats);
  const tokenTab = useS((state) => state.tokenTab);
  const observabilityRuns = useS((state) => state.observabilityRuns);
  const observabilityHealth = useS((state) => state.observabilityHealth);
  const observabilityIncidents = useS((state) => state.observabilityIncidents);
  const runStartedAt = useS((state) => state.runStartedAt);
  const pendingApproval = useS((state) => state.pendingApproval);
  const approvalIndex = useS((state) => state.approvalIndex);
  const approvalResolving = useS((state) => state.approvalResolving);
  const modelPicker = useS((state) => state.modelPicker);
  const skills = useS((state) => state.skills);
  const config = useS((state) => state.config);
  const currentSession = useS((state) => state.currentSession);
  const selectedModelProfile = useS((state) => state.selectedModelProfile);
  const welcomeNotice = useS((state) => state.welcomeNotice);
  const setupIndex = useS((state) => state.setupIndex);
  const setupFlow = useS((state) => state.setupFlow);
  const slashIndex = useS((state) => state.slashIndex);
  const skillsIndex = useS((state) => state.skillsIndex);
  const skillActionIndex = useS((state) => state.skillActionIndex);
  const hooksIndex = useS((state) => state.hooksIndex);
  const skillMentionIndex = useS((state) => state.skillMentionIndex);

  const activeSetupField = setupFieldAt(setupIndex, setupFlow);
  const slashItems = useMemo(() => filterSlash(buildSlashItems(), inputValue), [inputValue]);
  // Panel-scoped input is not chat input: typing "/" while a list panel is open
  // used to open the slash palette and take over the arrows, Tab and Esc that
  // the panel needs.
  const slashMenuOpen = mode === "chat" && acceptsComposerSubmission(panel) && inputValue.startsWith("/");
  const skillMentionMenuOpen = mode === "chat" && isSkillMentionQuery(inputValue);
  const skillMentions = useMemo(() => filterSkillMentions(skills, inputValue), [inputValue, skills]);
  const visibleSkills = useMemo(() => {
    if (panel === "skill-toggle") return filterSkills(skills, inputValue);
    if (panel === "skills") return skills.filter(isSkillEnabled);
    return skills;
  }, [inputValue, panel, skills]);

  const { done, active } = useMemo(() => splitTranscript(transcript), [transcript]);
  const staticItems = useMemo<StaticItem[]>(() => [{ kind: "hero", id: "hero" }, ...done], [done]);

  useEffect(() => {
    void bridge.boot();
  }, []);
  useEffect(() => {
    if (slashIndex >= slashItems.length) getState().set({ slashIndex: 0 });
  }, [slashIndex, slashItems.length]);
  useEffect(() => {
    if (skillsIndex >= visibleSkills.length) getState().set({ skillsIndex: 0 });
  }, [skillsIndex, visibleSkills.length]);
  useEffect(() => {
    if (skillMentionIndex >= skillMentions.length) getState().set({ skillMentionIndex: 0 });
  }, [skillMentionIndex, skillMentions.length]);
  const isFullScreenPanel = panel === "tokens" || panel === "runs";
  const prevFullScreenPanel = useRef(isFullScreenPanel);
  useEffect(() => {
    if (prevFullScreenPanel.current === isFullScreenPanel) return;
    prevFullScreenPanel.current = isFullScreenPanel;
    // Entering or leaving the full-screen tokens/runs panels unmounts and later
    // remounts <Static>, which reprints the whole transcript into scrollback.
    // Clear and bump the epoch on the transition so it repaints exactly once,
    // matching the /clear, /new, and /resume paths.
    clearTerminal();
    getState().set({ transcriptEpoch: getState().transcriptEpoch + 1 });
  }, [isFullScreenPanel]);
  const handleInputChange = useCallback(
    (value: string) => {
      // Typing ends history browsing, so the stale draft captured on the way
      // in cannot later overwrite what was just typed.
      exitHistoryBrowsing(getState());
      if (panel !== "chat") {
        getState().set({ inputValue: value, skillsIndex: 0 });
        return;
      }
      if (isSkillMentionQuery(value)) {
        getState().set({ inputValue: value, pendingSkill: null, skillMentionIndex: 0 });
        return;
      }
      getState().set({ inputValue: value });
    },
    [panel],
  );
  const handleChatSubmit = useCallback(
    (value: string) => {
      void submitChat(
        value,
        panel,
        busy,
        pendingSkill,
        skills,
        slashMenuOpen,
        slashItems,
        slashIndex,
        skillMentionMenuOpen,
        skillMentions,
        skillMentionIndex,
        exit,
      );
    },
    [
      busy,
      exit,
      panel,
      pendingSkill,
      skillMentionIndex,
      skillMentionMenuOpen,
      skillMentions,
      skills,
      slashIndex,
      slashItems,
      slashMenuOpen,
    ],
  );
  const handleSetupSubmit = useCallback(
    (value: string) => {
      void submitSetup(
        value,
        activeSetupField,
        Boolean(config?.has_api_key || config?.routes?.interactive?.has_api_key),
        setupIndex,
        setupFlow,
      );
    },
    [activeSetupField, config?.has_api_key, config?.routes?.interactive?.has_api_key, setupFlow, setupIndex],
  );

  useShellInput({
    mode,
    setupFlow,
    busy,
    compressing,
    inputLocked,
    viewMode,
    slashMenuOpen,
    slashItems,
    slashIndex,
    skills: visibleSkills,
    skillsIndex,
    skillActionIndex,
    hooksIndex,
    skillMentionMenuOpen,
    skillMentions,
    skillMentionIndex,
    panel,
    pendingSkill,
    configConfigured: Boolean(config?.configured),
    exit,
    bridge,
    getState,
  });

  return (
    <Box flexDirection="column">
      {panel !== "tokens" && panel !== "runs" ? (
        <Static key={`transcript-${transcriptEpoch}`} items={staticItems}>
          {(item) => (
            <Box key={item.id} flexDirection="column" paddingX={2}>
              {item.kind === "hero" ? (
                <HeroPanel />
              ) : (
                <TranscriptEntryView entry={item} viewMode={viewMode} highlighter={highlighter} />
              )}
            </Box>
          )}
        </Static>
      ) : null}
      <Box flexDirection="column" paddingX={2}>
        <ShellContent
          mode={mode}
          panel={panel}
          active={active}
          viewMode={viewMode}
          welcomeNotice={welcomeNotice}
          highlighter={highlighter}
          tokenStats={tokenStats}
          tokenTab={tokenTab}
          observabilityRuns={observabilityRuns}
          observabilityHealth={observabilityHealth}
          observabilityIncidents={observabilityIncidents}
        />
      </Box>
      {panel !== "tokens" && panel !== "runs" ? (
        <Box flexDirection="column" paddingBottom={1} paddingX={2}>
          <ShellFooter
            activeSetupField={activeSetupField}
            approvalIndex={approvalIndex}
            baseUrl={baseUrl}
            approvalResolving={approvalResolving}
            modelPicker={modelPicker}
            busy={busy}
            compressing={compressing}
            inputLocked={inputLocked}
            config={config}
            currentSession={currentSession}
            selectedModelProfile={selectedModelProfile}
            inputValue={inputValue}
            mode={mode}
            panel={panel}
            onChatSubmit={handleChatSubmit}
            onInputChange={handleInputChange}
            onSetupSubmit={handleSetupSubmit}
            pendingApproval={pendingApproval}
            pendingSkill={pendingSkill}
            queuedMessages={queuedMessages}
            slashIndex={slashIndex}
            slashItems={slashItems}
            slashMenuOpen={slashMenuOpen}
            skillMentionIndex={skillMentionIndex}
            skillMentionMenuOpen={skillMentionMenuOpen}
            skillMentions={skillMentions}
            statusLine={statusLine}
            runStartedAt={runStartedAt}
            turnTokenUsage={turnTokenUsage}
            tokenUsage={tokenUsage}
            contextUsage={contextUsage}
            toolActivity={toolActivity}
            turnCount={turnCount}
            viewMode={viewMode}
          />
        </Box>
      ) : null}
    </Box>
  );
}

const app = render(
  <InkPictureProvider>
    <SmithApp />
  </InkPictureProvider>,
  { exitOnCtrlC: false },
);
void app.waitUntilExit().then(
  () => process.exit(0),
  () => process.exit(1),
);
