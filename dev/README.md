# dev

Development workflow skills for Claude Code, Codex, opencode, and Pi.
The durable handoff is the code, its tests, the active plan under `.dev/{plan-name}/`, and relevant project records.
Skills are explicitly invoked and recommend the next step; only `yolo` follows scope, build, and ship inline in one session, and only `remote` runs build and ship elsewhere, in a fleet sandbox.

## Choose a skill

| Skill | Use when | Deliverable |
| --- | --- | --- |
| [scope](skills/scope/SKILL.md) | The problem or consequential choices need planning. | A settled spec with invariants, argued choices, and layer-tagged scenarios. |
| [build](skills/build/SKILL.md) | A spec is ready to implement. | Code, tests, runner receipts, incremental notes, and actual application evidence. |
| [ship](skills/ship/SKILL.md) | A change needs hardening, independent review, and an authorized PR. | Required checks, verified findings with explicit completeness, and current evidence. |
| [yolo](skills/yolo/SKILL.md) | One task should run unattended. | The full scope/build/ship contracts, recorded assumptions, and a PR or local handoff within available authority. |
| [remote](skills/remote/SKILL.md) | A scoped plan should build and ship in a sandbox on a fleet host, not on this machine. | The sandbox's commits, plan files, evidence, and run journal brought home, then a PR published from here. |
| [commit](skills/commit/SKILL.md) | Requested work is ready for local commits. | Atomic commits with what/why messages; a push only when requested. |

## Shared contracts

- [Execution authority](references/execution.md) owns permissions and durable-write authority.
  A skill never overrides session or repository restrictions.
- [Plan layout](references/plan-layout.md) owns paths and incremental working state.
- [Scenario evidence](skills/build/references/test-evidence.md) maps spec scenarios to current passing runner cases.
  The checker verifies receipts and declared layers; reviewers judge assertions and actual layer suitability.
- [Review transport](skills/ship/references/orchestration.md) preserves independent lens and verifier batches with bounded host concurrency.
  Missing or invalid results yield INCOMPLETE, not approval; useful findings remain visible.
- [Decision ledger](references/decision-ledger.md) records choices and rationale.
  [Contracts](references/contracts.md) record boundary guarantees with provenance; planned guarantees remain unverified until checked against implementation.
- [Architecture](references/architecture.md) and [dependency rules](references/dependency-rules.md) describe existing boundaries when a project keeps them.
  A missing overview does not turn a small task into whole-system documentation work.
- [CI parity](references/ci-parity.md) keeps every existing required repository check mandatory at the change's impact, with full PR follow-through when publication is authorized.
- [Reports](references/reporting.md) are local HTML artifacts.
  Share only when requested; evidence records the actual capture and tested sources.

Scope-review is optional; a settled spec can go directly to build.
Quick modes retain the same authority and evidence rules while reducing review machinery.
Supplementary hardening analyses need meaningful changed-code targets, and skips are reported as skips.
Mutation testing retains the configured, focused survivor-triage policy; a kill-rate score is not a ship gate.
Whole-system architecture capture, ledger seeding, and reverse audits are conditional tasks, not prerequisites to ordinary implementation.

## Run metrics

`scope`, `build`, `ship`, and `yolo` each start by snapshotting the run with `scripts/skill-metrics.py start` and end by printing what it measured: wall time, tokens split between the orchestrator and its subagents, agents dispatched, tool calls, the git delta since the snapshot, and any counters the skill tallied from tool output.
The numbers come from git and from the session store of whichever harness ran the skill, never from the model's recollection.
Claude Code, Codex, opencode, and Pi each name the running session to the shell tool (`CLAUDE_CODE_SESSION_ID`, `CODEX_THREAD_ID`, `OPENCODE_SESSION_ID`, `PI_SESSION_FILE`), and the script anchors on its own `start` call inside that session, so a nested harness or a phase run inside a subagent is measured where it ran.
A value a harness does not record prints as `n/a`, never as zero: Pi's review panel runs with `--no-session`, so Pi runs show subagent tokens and agents as `n/a`, and a run outside any of the four shows every session-derived value as `n/a`.
Every run appends a row to `.dev/metrics.jsonl` in the consuming repository, and the table compares the run against the median of earlier runs of the same skill, which is where a skill's cost and catch rate become visible over time.
The same call appends an entry to the cross-repository run journal under `~/.dev-workflow/memory/journal/` (or `$DEV_MEMORY_DIR`): the friction signals measured from the transcript (interrupts, denied and failed tool calls, the user's own turns, and how often each deterministic checker ran and failed), each with its transcript line, plus at most three `--friction` lines in which the skill names where the run fought its own instructions.
It stays on the machine; set `DEV_MEMORY_DIR=off`, or `"memory": {"enabled": false}` in `.dev/config.json`, to keep a repository out of it.

## Remote execution

`remote` splits the workflow across machines: `scope` runs here, with the user and the MCP servers; `build` and `ship` run unattended in a sandbox workspace on a fleet host, and the sandbox opens the PR with git credentials the host allocated to it.

```text
laptop: /dev:scope ──▶ /dev:remote ── git push ──▶ fleet host exchange clone ── fleet sync ──▶ ws-N: claude -p /dev:build, then /dev:ship ──▶ GitHub PR
laptop: review     ◀── /dev:remote collect ◀── git fetch ◀── fleet fetch ◀────────────── commits, plan files, reports, journal
```

Each phase is a fresh headless Claude Code session in the sandbox, run as a transient systemd unit under [the unattended contract](skills/remote/references/unattended.md): yolo's no-question rules and hard stops, commit everything, push only the work and evidence branches.
Dispatch probes the workspace first (a dry-run push to `origin` and `gh auth status`); a sandbox that passes runs ship's pull-request phase in full, and one that fails, or a dispatch with `--publish laptop`, stops at a prepared `pr.md` that is published from here after collect.
Collect fetches the branch by fast-forward only, merges the plan files without overwriting local edits, imports the run journal, reports the PR the sandbox opened, and opens a worktree of the branch here for publication or CI fixes.
Each workspace takes one run at a time, so three sandboxes build three plans in parallel while planning continues here.
[The protocol](skills/remote/references/protocol.md) owns what crosses each boundary and the run states.

Set it up once with `~/.dev-workflow/remote.toml`:

```toml
host = "user@fleet-host"            # ssh destination; key-based, BatchMode
sources_root = "/srv/agent-sources" # the host's exchange clones (fleet sourcesRoot)
workspaces = ["ws1", "ws2", "ws3"]  # fleet names to use; empty = every running ws-*
phase_timeout_hours = 8
model = ""                          # optional --model for the sandbox agent
publish = "auto"                    # auto: the sandbox publishes when its credentials pass the probe; sandbox; laptop
```

The host contract is the Incus workspace fleet: a `fleet` CLI with `sync`, `fetch`, `start`, and `view`; `incus` for `exec` and `list`; workspaces running `ws-{name}` VMs with an `agent` user, Claude Code, Python 3, and systemd.
For sandbox publication, the host also allocates git credentials: as the agent user, `git push` to the repository's `origin` URL and `gh` must work without a prompt.
A fine-grained token scoped to the fleet's repositories with contents and pull-request write is the narrowest credential that does; how it reaches the workspace (a token file, a credential helper, a relay that adds it on the wire) is the host's choice.
The sandbox agent runs on whatever model the fleet's gateway serves, so a remote run's quality follows that model, not this machine's.
`python3 dev/skills/remote/scripts/remote.py doctor` checks the setup; `status`, `logs --follow`, `stop`, `resume`, and `release` work from any terminal, without a session.

## Claude Code installation

```bash
/plugin marketplace add tobrun/workflow
/plugin install dev@nurbot
```

Invoke skills as `/scope`, `/build`, and so on.

## Codex installation

```bash
codex plugin marketplace add tobrun/workflow
codex plugin add dev@nurbot
```

Invoke skills as `$dev:scope`, `$dev:build`, and so on.

## opencode installation

opencode reads Claude-format `SKILL.md` files natively, so no generated distribution is needed.
Clone this repository anywhere and, from its root, symlink the source skills into opencode's global skill directory:

```bash
git clone https://github.com/tobrun/workflow
cd workflow
mkdir -p ~/.config/opencode/skills
for skill in "$PWD"/dev/skills/*/; do
  ln -sfn "${skill%/}" ~/.config/opencode/skills/"$(basename "$skill")"
done
ln -sfn "$PWD/dev/references" ~/.config/opencode/references
```

The last symlink keeps the shared references (`decision-ledger.md`, `contracts.md`, `architecture.md`) reachable through the `../../references/` links inside the skills.
Symlinks mean a `git pull` updates the skills in place; restart opencode afterwards, since skills load at startup.
The symlinks point at the clone, so keep it in place; if you move it, rerun the commands from the new location.

opencode has no `disable-model-invocation` field (it is ignored harmlessly); skills load through a model-invoked `skill` tool.
Preserve the explicit-invocation policy with a permission rule in `~/.config/opencode/opencode.json`:

```json
{
  "permission": {
    "skill": {
      "*": "allow",
      "scope": "ask",
      "commit": "ask",
      "build": "ask",
      "ship": "ask",
      "yolo": "ask",
      "remote": "ask"
    }
  }
}
```

Invoke a skill by asking for it by name, for example "run the scope skill on this request"; the permission rule makes opencode confirm before loading one.
Verify discovery with `opencode debug skill`.
`ship` runs its review panel through opencode's native `task` subagents.

## Pi installation

```bash
pi install git:github.com/tobrun/workflow
```

Invoke skills as `/skill:scope`, `/skill:build`, and so on. Pi loads the
source skills directly. A `ship` review phase starts multiple model processes,
one per selected lens and then one per non-trivial finding verifier, so its
model usage scales with the panel size.

## Workflow configuration

Optionally create `.dev/config.yaml` at the target repository root:

```yaml
mutation_testing: false
```

This skips mutation testing in `ship`, including when run by `yolo`.
With no file or no `mutation_testing` key, it runs by default; set the boolean to `true` to enable it explicitly.
Enabled runs select changed decision-heavy logic before acquiring a tool. A glue-only change skips acquisition and execution and reports `skipped (no changed decision-heavy logic)`, without counts.
A skipped check is reported as skipped, and all other checks and CI parity still run.
Invalid YAML, a non-mapping root, or a non-boolean value stops the ship gauntlet with an error.
Commit `.dev/config.yaml` to share the settings across contributors and CI.
Ignore the remaining `.dev/` contents with this root `.gitignore` block, replacing all blanket `.dev/` or `/.dev/` entries:

```gitignore
/.dev/*
!/.dev/config.yaml
```

YOLO verifies and repairs this policy during preflight.
The separate `.dev/config.json` run-journal preference remains local.
