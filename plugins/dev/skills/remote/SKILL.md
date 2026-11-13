---
name: remote
description: Run build and ship for a scoped plan in a sandbox workspace on a remote fleet host while planning and MCP access stay on this machine - dispatch the plan to a free sandbox, follow its progress, let the sandbox open the PR with its own git credentials (or publish from here when it has none), and collect the branch, plan files, and evidence back. Use when the user wants build or ship executed remotely, on a sandbox or the desktop, or in parallel with other work.
---

# Remote

Planning stays here with the user and the MCP servers; build and ship run unattended in a sandbox workspace on the fleet host.
A sandbox whose git credentials can push to `origin` publishes the PR itself, as ship would here; one without them stops at a prepared `pr.md`, and publication comes back here.
`{remote}` is `python3 {remote-skill-root}/scripts/remote.py`.
It owns every transfer, lock, and state change, so never copy code, plans, or results by hand, and never work around one of its refusals.
[references/protocol.md](references/protocol.md) owns what crosses each boundary, the run states, and the plan merge; [references/unattended.md](references/unattended.md) is the contract every sandbox session runs under.

Follow [../../references/execution.md](../../references/execution.md).
Dispatching `ship` or `ship-quick` is a request to ship, pull-request phase included, exactly as invoking it here would be; when the user says "no PR", "local only", or "I'll publish", dispatch with `--publish laptop`.

## Choose the mode

| The user says | Mode |
| ------------- | ---- |
| a plan name, "run it remotely", "build and ship on a sandbox" | Dispatch with `build,ship` |
| "build only remotely" | Dispatch with `build` |
| accepted review fixes are scoped with `scope-quick` | Dispatch with `build,ship-quick` |
| "status", "how is it going", "logs" | Follow |
| "collect", "bring it back", a finished run | Collect and publish |
| "resume", "stop", "release" | the matching command on the named run |

## Dispatch

1. Run `{remote} doctor`.
   No configuration: ask the user once for the fleet host's SSH destination and write `~/.dev-workflow/remote.toml` with `host = "..."`; every other key has a default (see the script header).
   A failed check is reported as is; do not dispatch around it.
2. Locate the plan per [../../references/plan-layout.md](../../references/plan-layout.md).
   Build needs a settled spec: if `spec.md` is missing or its change plan is empty, recommend `scope` and stop.
3. Run `{remote} dispatch --plan {plan-name} --phases {phases}`, adding `--workspace {name}` only when the user named one.
   Dispatch probes the workspace (a dry-run push to `origin` and `gh auth status`) and prints `publish`: `sandbox`, or `laptop` with the reason.
   `--publish sandbox` refuses instead of falling back; `--publish laptop` skips the probe.
   On a refusal, fix its cause within authority and run it again.
   Committing pending work is a commit request the user has to make; an ignore-policy repair follows plan-layout's ignore policy.
4. Report the run id, workspace, branch, base, phases, who publishes (with the reason when it is not the sandbox), and the noVNC view URL (the host's `fleet view` prints it; the password lives on the host under `~/.local/state/fleet/{name}/vnc.pass`).
   List any untracked files the script says were not sent.
5. When the host can notify on background completion, run `{remote} wait {run}` in the background and continue with Collect when it finishes.
   Otherwise tell the user the run continues without this session and that `/dev:remote collect` brings it home.

Up to one run per workspace runs at a time; dispatching several plans in a row fills the free sandboxes.

## Follow

- `{remote} status` lists the uncollected runs with state, phase, and idle time; `--all` adds finished ones.
- `{remote} logs {run} --tail 60` renders the current phase's session; `--follow` streams it; `--raw` gives the stream-json lines.
- An idle time far above the phase's usual pace, with the same last event, is a stall: read the logs before acting.
  `stop` then `resume` continues the same session with its working tree intact.

## Collect and publish

1. `{remote} status {run}` must show `done`, `failed`, `stopped`, or `lost`.
   For `failed` or `lost`, read `{remote} logs {run} --tail 80` and the `reason` first.
   A transient cause (gateway error, timeout with steady progress, VM restart) is a `resume`; a run that ended on a real blocker is collected as it stands.
   A `build gate` reason means build twice failed to map its scenarios to passing tests; read `gate-build.txt` after collect and treat it as a blocker, not a resume.
2. Run `{remote} collect {run}` and read its JSON summary: commits, branch update, plan updates and conflicts, uncommitted patches, review verdict, open calls, `publish`, `pr`, worktree.
   A refusal on a diverged branch or a dirty checkout leaves the sandbox's commits at `refs/dev-remote/{run}`; report it and stop.
3. Report plan conflicts (`{name}.remote` beside the local file) and any `uncommitted-*.patch`; never apply either without the user.
4. Work in the printed worktree from here on.
   Read `pr.md`, the latest `review_N.md`, and `implementation-notes.md`; they are the run's account of itself, written by a model nobody supervised.
   Run `python3 {remote-skill-root}/../ship/scripts/pr-evidence.py check .dev/{plan-name}/pr.md --require-current --repo-root {worktree}` and treat a failure as a refreshed-evidence task, not a formality.
5. When `pr` is set, the sandbox published: run `gh pr view {pr.url} --json isDraft,statusCheckRollup` and compare it with the run.
   A ready PR from a run that did not end `done`, or with a failing evidence check or open calls, goes back to draft (`gh pr ready --undo`); then continue at step 7.
   When `publish` was `sandbox` but `pr` is empty, the sandbox's publication failed: read the ship log's end for why, then publish from here as in step 6.
6. Otherwise publish within authority, following ship's [pull request](../ship/references/pull-request.md) reference from Evidence on: `pr-evidence.py publish /tmp/{repo}/pr-evidence` when images were extracted, push, then create or update the PR with `--body-file .dev/{plan-name}/pr.md`.
   Open it as a draft when the run did not end `done`, the verdict is not PASS or CONCERNS, `pr.md` lists open calls, or a check was deferred or remote-only.
   Without push or PR authorization, stop at the prepared body and the worktree path, and say what is left.
7. Follow the PR's required checks to green per [../../references/ci-parity.md](../../references/ci-parity.md), fixing in the worktree.
   A failure that needs more than a contained fix goes back through `scope-quick` here and a `build,ship-quick` dispatch.

## Wrap up

End with one message: the run id and how it ended, the commits brought home, the review verdict and the `review_N.md` path, the PR URL with draft or ready state and its checks (or what publication is waiting on), plan conflicts and uncommitted patches if any, and the open calls in the order the reviewer should read them.
Recommend the next step; never launch another skill.
