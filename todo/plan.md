# Execution Plan: Claude Code Skills Repository as a Plugin Marketplace

## Audience

This plan is written for a junior engineer.
Every phase has explicit acceptance criteria and a validation step.
Do not move to the next phase until the current phase passes its acceptance criteria.

## Goal

Build a Claude Code skills repository at `~/ws/workflow` that conforms to the plugin marketplace concept used by `~/ws/claude-plugins`.
The repository is a monorepo: one marketplace manifest at the root, and one directory per plugin.
Each plugin packages one or more skills.
The repository must be installable via `claude plugin marketplace add` and self-validating via a check script and CI.

## Reference Model

Study `~/ws/claude-plugins` before writing any code.
The structure to replicate:

```
.
├── .claude-plugin/
│   └── marketplace.json          # Marketplace manifest listing all plugins
├── {plugin-name}/                # One directory per plugin
│   ├── .claude-plugin/
│   │   └── plugin.json           # Plugin metadata (name, version, description, author)
│   ├── README.md                 # What the plugin does and how to use it
│   └── skills/
│       └── {skill-name}/
│           ├── SKILL.md          # Skill definition with YAML frontmatter
│           ├── references/       # Optional: deep-dive docs loaded on demand
│           ├── examples/         # Optional: worked examples
│           └── scripts/          # Optional: helper scripts the skill invokes
├── README.md                     # Marketplace overview with plugin table
├── CLAUDE.md                     # Guidance for Claude Code working in this repo
├── scripts/
│   └── validate.sh               # Self-validation script (built in Phase 3)
└── .github/
    └── workflows/
        └── validate.yml          # CI running validate.sh on every PR
```

Key conventions observed in the reference repo:

- `marketplace.json` has `name`, `owner`, `metadata.description`, and a `plugins` array where each entry has `name`, `source` (relative path like `./my-plugin`), and `description`.
- Every `plugin.json` has `name`, `version` (semver), `description`, and `author.name`.
- The plugin directory name, the `name` in `plugin.json`, and the `name` in the marketplace entry must all match.
- Every skill lives at `{plugin}/skills/{skill-name}/SKILL.md` with YAML frontmatter containing at minimum `name` and `description`.
- The root README contains a table listing every plugin with a "Use When" column and a "Tools" column.

Also read the official docs before starting:

- https://docs.claude.com/en/docs/claude-code/plugins-reference.md
- https://docs.claude.com/en/docs/claude-code/skills.md

## Phase 0: Repository Bootstrap

### Tasks

1. Initialize a git repository in `~/ws/workflow` if not already one.
2. Create `.gitignore` covering OS junk (`.DS_Store`), editor files, and `node_modules` if any tooling is added later.
3. Create the root `README.md` with a short description and an empty plugin table (fill it in as plugins land).
4. Create `CLAUDE.md` describing the repository structure and the rule that all changes must pass `scripts/validate.sh`.

### Acceptance Criteria

- [ ] `git status` works inside the repo.
- [ ] `README.md` and `CLAUDE.md` exist and describe the marketplace concept.
- [ ] No stray files at the root other than the documented ones.

### Validation

Run `ls -la` and confirm the layout matches the structure diagram above (minus plugins, which come later).

## Phase 1: Marketplace Manifest

### Tasks

1. Create `.claude-plugin/marketplace.json` at the repo root.
2. Fill in `name` (e.g. `tobrun-skills`), `owner.name`, `metadata.description`, and an empty `plugins` array.

### Acceptance Criteria

- [ ] `jq . .claude-plugin/marketplace.json` parses without error.
- [ ] The manifest contains `name`, `owner`, `metadata.description`, and `plugins`.
- [ ] `claude plugin marketplace add ~/ws/workflow` succeeds locally (an empty marketplace is valid).

### Validation

```bash
jq -e '.name and .owner.name and (.plugins | type == "array")' .claude-plugin/marketplace.json
claude plugin marketplace add ~/ws/workflow
claude plugin marketplace list
```

Remove the test registration afterwards with `claude plugin marketplace remove <name>` so local state stays clean.

## Phase 2: First Plugin with One Skill

Build one complete plugin end to end before adding more.
This proves the whole pipeline works and gives you a template for every later plugin.

### Tasks

1. Pick a small, real skill you understand well (e.g. a repo-specific workflow or a checklist you actually use).
2. Create `{plugin-name}/.claude-plugin/plugin.json` with `name`, `version: "0.1.0"`, `description`, and `author`.
3. Create `{plugin-name}/skills/{skill-name}/SKILL.md` with YAML frontmatter:

```yaml
---
name: skill-name
description: One sentence saying what the skill does and when Claude should use it.
---
```

4. Write the skill body: concise instructions, with heavy or rarely-needed detail split into `references/` files that the SKILL.md points to.
5. Create the plugin `README.md` explaining what it does and how to trigger it.
6. Add the plugin to the `plugins` array in `marketplace.json` with a matching `name`, a `./` relative `source`, and a `description`.
7. Add a row to the root README plugin table.

### Acceptance Criteria

- [ ] Directory name, `plugin.json` name, and marketplace entry name are identical.
- [ ] `SKILL.md` frontmatter parses as YAML and contains `name` and `description`.
- [ ] The skill description states both what it does and when to use it (this is what triggers skill selection).
- [ ] `SKILL.md` body is under roughly 150 lines; anything longer belongs in `references/`.
- [ ] Plugin README exists and is accurate.
- [ ] Root README table has a row for the plugin.

### Validation (end-to-end test)

1. `claude plugin marketplace add ~/ws/workflow`.
2. `claude plugin install {plugin-name}@{marketplace-name}`.
3. Start a fresh Claude Code session and confirm the skill appears in the available skills listing.
4. Give Claude a prompt that should trigger the skill and confirm the skill content is loaded and followed.
5. Give Claude an unrelated prompt and confirm the skill does not trigger.
6. Uninstall and re-install to confirm the flow is repeatable.

If any step fails, fix the plugin and repeat all six steps from the top.

## Phase 3: Self-Validation Script

Write `scripts/validate.sh` so every structural rule from Phases 1 and 2 is checked automatically.
This script is the heart of the self-validation loop: humans and agents both run it after every change.

### Checks the script must implement

1. `marketplace.json` exists and is valid JSON (`jq -e`).
2. Every entry in `plugins[]` has `name`, `source`, and `description`.
3. Every `source` path exists as a directory.
4. Every plugin directory in the repo is listed in `marketplace.json` (no orphans in either direction).
5. Every plugin has `.claude-plugin/plugin.json` with `name`, `version` matching semver, `description`, and `author.name`.
6. Plugin directory name equals `plugin.json` name equals marketplace entry name.
7. Every plugin has a `README.md`.
8. Every plugin has at least one of `skills/`, `commands/`, or `agents/`.
9. Every `skills/*/` directory contains a `SKILL.md`.
10. Every `SKILL.md` starts with `---`, has parseable YAML frontmatter, and the frontmatter contains non-empty `name` and `description`.
11. The `name` in SKILL.md frontmatter matches its directory name.
12. Every plugin appears in the root README table.
13. No file in the repo contains an em dash (house style rule).

Exit non-zero on the first category of failure and print every violation with its file path, so failures are actionable.
Use only tools available on a stock macOS/Linux dev machine plus `jq` (and `yq` or a small Python snippet for YAML parsing).

### Acceptance Criteria

- [ ] `scripts/validate.sh` passes on the current repo.
- [ ] Deliberately breaking each rule (13 rules, 13 experiments) makes the script fail with a clear message naming the offending file.
- [ ] The script runs in under 10 seconds.

### Validation

For each rule: introduce the violation, run the script, confirm it fails with a useful message, revert the violation, confirm it passes again.
Keep a scratch checklist of the 13 experiments and tick them off.

## Phase 4: CI

### Tasks

1. Create `.github/workflows/validate.yml` that runs `scripts/validate.sh` on every push and pull request.
2. Pin the runner to `ubuntu-latest` and install `jq`/`yq` if not preinstalled.

### Acceptance Criteria

- [ ] The workflow passes on a clean branch.
- [ ] A PR that violates any Phase 3 rule fails CI.

### Validation

Push a branch with a deliberate violation, open a draft PR, confirm the red X, then fix it and confirm the green check.
Close the draft PR afterwards.

## Phase 5: Grow the Catalog

Repeat Phase 2 for each additional skill or plugin.
Group related skills into one plugin (like `mapbox-gitops` bundles four gitops skills) instead of making one plugin per skill, unless the skill stands alone.

### Per-plugin Definition of Done

- [ ] Phase 2 acceptance criteria all pass for the new plugin.
- [ ] `scripts/validate.sh` passes.
- [ ] The Phase 2 end-to-end test (install, trigger, negative trigger, reinstall) passes.
- [ ] Version bumped in `plugin.json` on every subsequent change to the plugin.

## The Self-Validation Loop

Follow this loop for every change to the repository, no exceptions:

1. Make the change.
2. Run `scripts/validate.sh`; fix until green.
3. If the change touches skill content, run the end-to-end test: install the plugin from the local marketplace in a fresh session, trigger the skill with a realistic prompt, and confirm the behavior matches the skill's intent.
4. If the skill did not trigger or behaved wrong, treat it as a bug: improve the `description` (trigger problems) or the body (behavior problems), then repeat from step 2.
5. Commit only when both the script and the end-to-end test pass.
6. CI re-runs the script as a backstop, but never rely on CI as the first line of defense.

## Common Pitfalls

- A skill that never triggers almost always has a vague `description`. The description must say when to use it, not just what it is.
- A `SKILL.md` that dumps everything into the body wastes context. Move detail into `references/` and link to it.
- Forgetting to add a new plugin to `marketplace.json` or the README table. The validate script catches both; run it.
- Name mismatches between directory, `plugin.json`, and the marketplace entry. Pick the name once and copy it exactly.
- Editing an installed copy of the plugin instead of the repo. Always edit in `~/ws/workflow`, then reinstall to test.

## Estimated Effort

- Phase 0-1: half a day.
- Phase 2: one day for the first plugin, including learning the install/test loop.
- Phase 3: one day, including the 13 break-it experiments.
- Phase 4: two hours.
- Phase 5: ongoing; roughly half a day per new skill once the template exists.
