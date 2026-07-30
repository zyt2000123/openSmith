# Bundled upstream skill sources

These files are intentionally vendored into Agent-Smith so the three coding
chains do not depend on a global Codex installation.  The pinned revision is
the reproducibility boundary; upgrades are an explicit dependency change.

| Bundled skill | Upstream | Pinned revision | License |
| --- | --- | --- | --- |
| `grill-me`, `grilling`, `research`, `diagnosing-bugs`, `code-review` | [mattpocock/skills](https://github.com/mattpocock/skills) | `2ab958093e83e0ec752e6c1c5932da465bf23e0c` | MIT |
| `tdd-workflow`, `verification-loop` | [affaan-m/ECC](https://github.com/affaan-m/ECC) | `e4e4163101f162881e628f300a9ca4e6a940bcea` | MIT |
| `ecc-plan` | [`commands/plan.md` in affaan-m/ECC](https://github.com/affaan-m/ECC/blob/e4e4163101f162881e628f300a9ca4e6a940bcea/commands/plan.md) | `e4e4163101f162881e628f300a9ca4e6a940bcea` | MIT |

`ecc-plan` is the only registration adapter: ECC publishes it as a command,
while Agent-Smith's registry requires a directory containing `SKILL.md`.  Its
methodology is the upstream command body; its extra frontmatter only makes it
loadable.  Pipeline-specific compatibility instructions live in pipeline YAML,
not in the vendored source skills.

## Closed runtime boundary

The three pipelines use only the listed skill directories and the tool scopes
declared beside each pipeline node.  Optional upstream host integrations —
subagents, slash commands, issue-tracker setup, Plan Canvas, `.claude` plan
artifacts, global package-manager settings, and package installation helpers —
are intentionally not bundled or invoked by these chains.

The one useful local helper already carried with its source is
`diagnosing-bugs/scripts/hitl-loop.template.sh`.  ECC's
`scripts/setup-package-manager.js` belongs to its wider host plugin and cannot
run from an arbitrary target workspace when only a skill is vendored; the TDD
node therefore derives commands from the target workspace's own manifests,
lock files, CI files, and test configuration instead.

| Upstream reference | Agent-Smith support decision |
| --- | --- |
| `grill-me` → `grilling` | Both source skills are bundled; the visible entry is routed into the bundled `grilling` node. |
| Research primary sources | The research node exposes only bundled `web_search` and `web_fetch`; it never needs an external research agent or crawler. |
| `diagnosing-bugs` HITL loop | The source template is bundled alongside the skill. |
| ECC package-manager helper | Deliberately not bundled: commands are discovered from the target workspace, so no global ECC host/plugin dependency is created. |
| Subagents, slash commands, Plan Canvas, issue trackers, `.claude` artifacts | Deliberately not bundled or invoked; each pipeline node contract forbids them. |
