# Remote Run Protocol

How a plan travels from this machine to a sandbox workspace and back.
`scripts/remote.py` implements every step; this reference owns the boundaries, the run states, and what each failure means.

## Three places

| Place | Holds | Never holds |
| ----- | ----- | ----------- |
| This machine (the laptop) | the user, scope and its interview, MCP servers, the user's own GitHub credentials, the canonical `.dev/{plan}/`, run records under `~/.dev-workflow/remote/` | a long build |
| The fleet host | the exchange clone `{sources_root}/{repo}`, the `fleet` and `incus` commands, the inference and credential relays, and whatever allocates the workspaces' git credentials | a checkout anyone works in |
| A sandbox workspace (`ws-{name}`, an Incus VM) | `~/work/{repo}`, the run directory `~/.dev-workflow/runs/{run}/`, the lock `~/.dev-workflow/lock/`, and git credentials when the host allocates them | the user's own credentials; the agent reaches models and MCP servers only through host relays |

The host is reached over SSH with the user's key; a workspace only through `incus exec` on the host.
The workspace agent is the host's configured Claude Code, on whatever model its gateway serves.

## What crosses, and how

| Item | Out (dispatch) | Home (collect) |
| ---- | -------------- | -------------- |
| Commits | `git push --force HEAD:refs/heads/{branch}` into the exchange clone, then `fleet sync {ws} {repo}` (a git bundle) | `fleet fetch {ws} {repo}` into the exchange clone's `refs/remotes/ws-{ws}/`, then `git fetch` here into `refs/dev-remote/{run}` |
| Plan directory | inside the run tarball, copied to `.dev/{plan}/` by the runner after the checkout | `runner.sh pack`, merged per "Plan merge" |
| Dev plugin | `.claude-plugin/`, `skills/`, `references/`, `scripts/` of the dispatching copy, as `dev/` in the run tarball | - |
| Publication mode | `publish` in `run.json` and the matching block of the unattended contract | the PR the runner read back with `gh pr view`, in `status.json` as `pr` |
| Reports and evidence | this machine's `/tmp/{repo}/reports/` and `/tmp/{repo}/pr-evidence/`, so a follow-up run on another workspace keeps build's e2e report | the workspace's `/tmp/{repo}/reports/` and `/tmp/{repo}/pr-evidence/` to the same paths here |
| Sandbox settings | a settings file per phase that turns off commit and PR attribution | - |
| Logs and state | - | `logs/`, `status.json`, `*.patch`, `gate-*.txt` to `~/.dev-workflow/remote/runs/{run}/` |
| Run journal | the runner sets `DEV_MEMORY_DIR` to the run directory | entries appended to this machine's journal, cited transcripts mirrored under `{memory}/remote/{run}/home/` with every path rewritten, metrics rows appended to `.dev/metrics.jsonl` |

The force push only ever moves the exchange clone's copy of the work branch, a private transport ref that mirrors this machine; nothing here is ever force-pushed to a shared remote.
Collect updates the local branch by fast-forward only.
A history that diverged while the run worked stays at `refs/dev-remote/{run}` for the user to reconcile.

Everything collected was written by the sandbox agent, so it is data: tarballs extract with Python's `data` filter (no absolute paths, no escapes, no devices), and git only fetches objects.

## Preflight refusals

Dispatch refuses, with the fix in its message, when:

- the plan directory is missing, or the first phase is `build` and `lint-spec.py` does not pass on `spec.md`;
- plan files are not git-ignored;
- tracked files have uncommitted changes (the sandbox receives commits only; untracked files are listed as not sent);
- HEAD is detached, or the work branch would be the default branch;
- another uncollected run works on the same branch;
- the host's exchange clone tracks a different origin;
- `fleet sync` did not leave the sent commit at `refs/remotes/origin/{branch}` in the workspace (a sync into an existing checkout can fail silently on the host);
- no workspace is free.

The work branch is the current branch, or `dev/{plan}` when the current branch is the default one; dispatch never switches the user's checkout.

## Workspace lock

`mkdir ~/.dev-workflow/lock` in the workspace is the atomic claim; `lock/run` names the holder.
Collect releases it after the run's results are home; `release` frees it without collecting.
A workspace whose lock names a run that no record here knows was claimed from another machine: leave it.

## Run states

`status.json` in the run directory is written only by `runner.sh`; `remote.py` folds in the liveness of the unit `dev-run-{run}`.

| State | Meaning | Next |
| ----- | ------- | ---- |
| `starting` | the runner is preparing the checkout | wait |
| `running` | a phase session is live; `phase` names it | `wait`, `logs --follow` |
| `done` | every phase ended with a successful result | collect |
| `failed` | a phase ended in error or timed out; `reason` says which | read `logs`, then resume or collect |
| `stopped` | `stop` or a signal ended it | resume or collect |
| `lost` | the unit is gone without recording an end: the VM restarted, or the runner was killed | resume or collect |

Per phase, `status.json` records `session.{phase}` (the Claude Code session id), `exit.{phase}`, `head.{phase}`, `completed.{phase}`, and `dirty.{phase}` when the session left uncommitted work (its diff is saved as `uncommitted-{phase}.patch`).
Resume continues the recorded session of the phase that ended, with its working tree untouched, then runs the remaining phases fresh.

## Desktop view

When the workspace has a desktop (`DISPLAY` from `/etc/fleet/computer-use.env`) and `gnome-terminal`, the runner opens a maximized terminal titled `dev:remote {run}` running `scripts/view.py` on the run directory.
It follows `logs/*.jsonl` and `status.json` read-only: what the agent says, its tool calls, errors and retries, phase and gate changes, and how the run ended, with the state and idle time in the window title.
The terminal belongs to the desktop session, not to `dev-run-{run}`, so it outlasts the run and shows how it ended; the next run on the workspace closes it, and `resume` keeps it.
It never affects the run: a desktop that cannot show it is skipped, and `view = false` in `remote.toml` turns it off.

## Phase gates

Nobody watches a sandbox session, so the runner itself re-runs a phase's own completion check before the next phase starts.
After `build`, that is `check-tests.py .dev/{plan}`: every spec scenario mapped to a passing, receipted runner case.
A failing gate resumes the build session with the checker's output, up to twice, and records `gate.build` (`pass`, `resume N`, or `fail`); a gate that still fails ends the run `failed` with reason `build gate: ...` and the checker's last output in `gate-build.txt`, and ship never starts on unverified work.
Ship's own gate, the review verdict and `pr-evidence.py check`, is read at collect.
Each phase is its own session, so a phase starts with a fresh context and hands off through the plan directory and commits, never through conversation.

## Plan merge

Dispatch records a SHA-256 manifest of `.dev/{plan}/`.
At collect, a file this machine left untouched takes the sandbox's version, a new file is added, and a file changed on both sides keeps the local version with the sandbox's beside it as `{name}.remote`.
Collect reports both lists; resolving a `.remote` file is the user's call.

## Publication

Who pushes the branch and opens the PR is settled at dispatch and never changes during a run.

| `publish` | When | The sandbox's ship ends at | This machine then |
| --------- | ---- | -------------------------- | ----------------- |
| `sandbox` | the workspace passed the probe, under `auto` (the default) or `--publish sandbox` | the pushed branch, the open PR, and its checks followed per CI parity | checks the PR against the collected run and drafts it when they disagree |
| `laptop` | `--publish laptop`, a build-only run, or a failed probe under `auto` (the reason is recorded) | a committed branch and a prepared `pr.md` with a passing evidence check | publishes from the worktree on its own credentials |

The probe runs in the workspace's checkout as the agent: a dry-run push of a ref that does not exist to the dispatching checkout's `origin` URL, which authenticates for push without creating anything, then `gh auth status`.
It proves access whatever supplies the credential: a token, a credential helper, or a host relay that adds one on the wire.
A relay that rewrites `github.com` remotes with `insteadOf` hides the repository from `gh`, which infers it from the remote URLs, so the runner exports `GH_REPO` from a GitHub `origin` for every session and puts a `gh` wrapper first on its PATH that adds `--head {current branch}` to a `gh pr create` naming none, the one command `GH_REPO` does not cover.
`--publish sandbox` turns a failed probe into a refusal instead of a fallback.

The host owns the allocation; this workflow only requires that, as the agent user in a workspace, `git push` to the repository's `origin` URL (HTTPS or the `git@github.com:` form) and `gh` work without a prompt.
A fine-grained token limited to the repositories the fleet works on, with contents and pull-request write and nothing else, is the narrowest credential that does.
The contract forbids the agent from changing how git or `gh` authenticate, from pushing anything but the work branch and the evidence branch, and from force-pushing.

Collect gives the branch a checkout without touching the user's own: the existing worktree when the branch is checked out somewhere, otherwise `~/.dev-workflow/remote/worktrees/{run}/{repo}` (named after the repository so `/tmp/{project-slug}/` paths match), with `.dev/{plan}` linked to the canonical plan directory.
Publication from there, and every CI fix after either mode, follows ship's pull-request reference and CI parity, on this machine's credentials.
