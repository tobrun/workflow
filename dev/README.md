# dev

Development workflow skills for Claude Code, Codex, opencode, and Pi.
The durable handoff is the code, its tests, the active plan under `.dev/{plan-name}/`, and relevant project records.
Skills are explicitly invoked and recommend the next step; only `yolo` follows scope, build, and ship inline in one session, and only `remote` runs that same session elsewhere, on a fleet instance.

## Choose a skill

| Skill | Use when | Deliverable |
| --- | --- | --- |
| [scope](skills/scope/SKILL.md) | The problem or consequential choices need planning. | A settled spec with invariants, argued choices, and layer-tagged scenarios. |
| [build](skills/build/SKILL.md) | A spec is ready to implement. | Code, tests, runner receipts, incremental notes, and actual application evidence. |
| [ship](skills/ship/SKILL.md) | A change needs hardening, independent review, and an authorized PR. | Required checks, verified findings with explicit completeness, and current evidence. |
| [yolo](skills/yolo/SKILL.md) | One task should run unattended. | The full scope/build/ship contracts, recorded assumptions, and a PR or local handoff within available authority. |
| [remote](skills/remote/SKILL.md) | A task or a scoped plan should run to its pull request on a fleet instance you pick, not on this machine. | A pull request the instance opened, and its branch and plan files brought home on request. |
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
  A missing overview does not turn a small task into whole-system documentation work.
- [CI parity](references/ci-parity.md) keeps every existing required repository check mandatory at the change's impact, with full PR follow-through when publication is authorized.
  Share only when requested; evidence records the actual capture and tested sources.

A settled spec goes directly to build; scope offers a deeper verified review for large or risky changes.
Quick modes retain the same authority and evidence rules while reducing review machinery.
Supplementary hardening analyses need meaningful changed-code targets, and skips are reported as skips.
Mutation testing retains the configured, focused survivor-triage policy; a kill-rate score is not a ship gate.

## Remote execution

`/dev:remote 1 {plan or task}` runs the work on the first fleet instance, `2` on the second: the user assigns the instance, and an instance holds one run.
A run is one unattended yolo session there, under [the unattended contract](skills/remote/references/unattended.md): it builds and ships a plan scoped here, or scopes a task itself first, and opens the pull request with git credentials the host allocated to the instance.

```text
laptop: /dev:remote 1 {plan} ── HEAD (git bundle), plan files, plugin ──▶ ws-ws1: claude -p /dev:yolo ──▶ GitHub PR
                                                                            ▲            │ session ended
                                                                            └── resume ──┘ until committed, pushed, PR open
laptop: /dev:remote 1 pull   ◀── work branch (git bundle), plan files ──────
```

The instance's runner decides when the run is finished, not the session: each time the session ends it checks that everything is committed, the branch is pushed, and the pull request is open at that commit, and resumes the same session with what is missing until it holds.
A run ends as `done`, as `blocked` when the session recorded a hard stop, or as `failed` once the configured number of sessions is spent.
`continue` puts a run that ended back to work in its own session and checkout, with an instruction when there is one: a CI failure, a resolved blocker, more work.
`pull` brings the work branch (fast-forward only) and the plan files (never over a local edit) to this checkout, so work an instance cannot do is finished here with `build` or `ship`.
This machine keeps no record of a run; everything about it lives on the instance and is addressed by the instance's number.
On the instance's desktop, a terminal follows the session live, so the noVNC view `fleet view` prints shows the run as it happens.

Set it up once with `~/.dev-workflow/remote.toml`:

```toml
host = "user@fleet-host"      # ssh destination; key-based, BatchMode
instances = []                # fleet names in order (1 is the first); empty = ws1, ws2, ...
model = ""                    # optional --model for the instance's agent
attempts = 6                  # sessions a run may take before it ends as failed
attempt_timeout_hours = 8     # one session's time limit
view = true                   # a terminal on the instance's desktop follows the run, for watching over noVNC
```

The host contract is the Incus workspace fleet: `incus` for `exec` and `list`, a `fleet` CLI for `start` and `view`, and `ws-{name}` VMs with an `agent` user, Claude Code, git, `gh`, Python 3, and systemd.
As the agent user, `git push` to the repository's `origin` URL and `gh` must work without a prompt; how the credential reaches the instance (a token file, a credential helper, a relay that adds it on the wire) is the host's choice.
The instance's agent runs on whatever model the fleet's gateway serves, so a remote run's quality follows that model, not this machine's.
`python3 dev/skills/remote/scripts/remote.py doctor` checks the setup; `status`, `logs N --follow`, `stop N`, `continue N`, and `pull N` work from any terminal, without a session.

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

The last symlink keeps the shared references (`decision-ledger.md`, `contracts.md`, `plan-layout.md`) reachable through the `../../references/` links inside the skills.
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
