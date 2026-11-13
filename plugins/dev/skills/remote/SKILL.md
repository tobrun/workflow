---
name: remote
description: Run a task or a scoped plan to its pull request on a fleet instance the user picks by number - `/dev:remote 1 {plan or task}` starts one unattended yolo run on the first instance, which keeps working until its pull request is open; the same number follows it, gives it more work, stops it, or brings its branch and plan files back here. Use when the user wants work run remotely, on an instance, a sandbox, or the desktop, or asks about a run there.
---

# Remote

The user picks the instance; the instance finishes the work.
A run is one unattended [yolo](../yolo/SKILL.md) session on that instance: it builds and ships a plan scoped here, or scopes a task itself first, and ends at a pull request it opened with the instance's own git credentials.
The instance's runner checks the deliverable every time the session ends - everything committed, the branch pushed, the pull request open at that commit - and resumes the same session with what is missing until it holds.
So a run ends by itself as `done`, or as `blocked` when the session wrote down a hard stop.

`{remote}` is `python3 {remote-skill-root}/scripts/remote.py`, and `{n}` is the instance: the number the user gave (`1` is the first instance) or its fleet name.
An instance holds one run, so `{n}` names the run too; this machine keeps no record of it.
The script owns every transfer and state change: never copy code, plans, or results by hand.
[references/unattended.md](references/unattended.md) is the contract the session runs under.

Follow [../../references/execution.md](../../references/execution.md).
Starting a run is a request to ship: it authorizes the instance to push the work branch and open its pull request, as yolo would here.

## What the user says

| The user says | Do |
| ------------- | -- |
| `{n}` and a plan name, or "run this plan on 2" | Start with `--plan` |
| `{n}` and a task, or a Markdown file holding one | Start with `--task` or `--task-file` |
| `{n}` alone | Start the plan this conversation scoped or names; with none, ask what to run |
| no instance | `{remote} status`, show it, and ask which instance; never pick one for the user |
| "status", "how is it going", "logs" | Follow |
| "continue", "also do ...", "fix the CI failure", "keep going" | Continue |
| "bring it back", "pull", "continue here", "finish it locally" | Pull |
| "stop" | `{remote} stop {n}` |

## Start

1. Run `{remote} start {n} --plan {plan-name}` for a plan under `.dev/` that has a `spec.md`, per [../../references/plan-layout.md](../../references/plan-layout.md), or `{remote} start {n} --task "{task}"` for a task.
   A plan takes `--task` too, as an extra instruction: a narrower slice, a constraint, what an earlier run left open.
   No configuration: ask the user once for the fleet host's SSH destination and write `~/.dev-workflow/remote.toml` with `host = "..."`; every other key has a default (see the script header).
2. It sends this checkout's HEAD, the plan directory, and this plugin, and the run works on the current branch, or on `dev/{plan-name}` when this checkout is on the default branch; this checkout is never switched.
   Only committed work is sent: report the `not_sent` lines, and when one of them is work the run needs, say so before the run has gone far - committing it is the user's call, and a new start sends it.
3. A refusal names its cause.
   A busy instance is the user's to resolve: report what runs there and let them stop it or name another.
4. Report the instance, run, branch, and the noVNC `view` URL, where a terminal on the instance's desktop follows the session live.
5. When the host can notify on background completion, run `{remote} wait {n}` in the background and go on to Wrap up when it ends.
   Otherwise say that the run finishes without this session and that `/dev:remote {n}` status or pull picks it up.

## Follow

- `{remote} status` lists every instance with its run, state, session count, idle time, branch, and pull request; `{remote} status {n} --json` gives one in full.
- `{remote} logs {n} --tail 60` renders the session; `--follow` streams it; `--raw` gives the stream-json lines.
  A `runner:` line says why the runner sent the session back to work.
- States: `starting`, `running`, `done`, `blocked` (a hard stop; `reason` holds it), `failed` (still unfinished after the configured number of sessions), `stopped`, `lost` (the runner vanished: the instance restarted), `idle` (no run).
- A long idle time with the same last event is a stall: read the logs, then `stop` and Continue.

## Continue

`{remote} continue {n}` puts a run that is not running back to work, in its own session and checkout, whatever state it ended in.
`--prompt "..."` says what to do: the CI failure to fix, the review point to address, the blocker that is now resolved, more work on a finished run.
Without a prompt it goes on from what is missing; a run that is already finished stays `done`.
A session whose transcript is gone starts a new one on the same checkout.
To redirect a running session, `stop` it first.
Then follow it as in Start step 5.

## Pull

`{remote} pull {n}`, from this repository's checkout, brings the run's commits and plan files here; it works in any state, and again after more work.

- The work branch is created here or fast-forwarded, in place when it is checked out and clean.
  A branch that diverged here, or a checkout with uncommitted changes, is left alone, with the instance's commits at `refs/dev-remote/{workspace}`: report it and let the user reconcile.
- Plan files the run changed land in `.dev/`.
  A file edited on both sides keeps this machine's version and gets the instance's beside it as `{name}.remote`; report those and never apply one without the user.
- To continue the work on this machine, pull, switch to the work branch with the user's consent, and recommend the stage the plan directory is at (`build` continues from `implementation-notes.md`, `ship` from its phase file); never launch it.
  A later start on an instance sends this machine's commits back out.

## Wrap up

When a run ends, pull it and read what it says about itself: `pr.md`, the latest `review_N.md`, `implementation-notes.md` - written by a model nobody supervised.

- `done`: run `gh pr view {pr.url} --json isDraft,statusCheckRollup` and report the checks.
  A ready pull request whose `open_calls` are not empty, or whose verdict is not PASS or CONCERNS, goes back to draft (`gh pr ready --undo`).
- `blocked`: report the `blocked` text.
  When the stop is a rejected push or `gh` call, publish from here within authority, following ship's [pull request](../ship/references/pull-request.md) reference: push the pulled branch and create the pull request with `--body-file .dev/{plan-name}/pr.md`.
  Any other stop is the user's to resolve; Continue then takes the answer as its prompt.
- `failed`, `stopped`, `lost`: read `{remote} logs {n} --tail 80` and the `reason`.
  A transient cause is a Continue; work the instance cannot do is finished here after the pull.

End with one message: the instance and how its run ended, the pull request URL with draft or ready state and its checks, the commits brought home, the review verdict and the `review_N.md` path, plan conflicts if any, and the open calls in the order the reviewer should read them.
Recommend the next step; never launch another skill.
