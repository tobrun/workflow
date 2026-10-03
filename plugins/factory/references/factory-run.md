# Factory Run Protocol

Owned by the `run` skill. Every phase copy (`scope`, `scope-review`, `build`, `ship`) reads this file for the result envelope and the unattended policy; `run/SKILL.md` also reads it for the run state schema and the launch prompt shape.

## Run state

`.dev/{plan}/factory-run.json`, written only by the `run` skill, via `run-state.py`. It exists from `init` on, before any branch. Fields:

- `schema` (currently `2`), `plan`, `request`, `base` (the default branch at `init`). A state file with no `schema` is read as the default four-phase pipeline, three attempts per phase and a ceiling of 12, so runs already in flight keep resuming.
- `branch`, `seals`, `sealed_nothing`, `scenario_texts` (per change set), `not_doing_lines` (the `⊘` lines), `config_path`, `config_sha256`, `pipeline`, `dirty_files` - all written at `handoff`. `seals` lists `{path, sha256, notation}` per sealed file: the paths come from the `seals` of the phases that ran before the go, and `notation` marks a file in the dev spec notation, whose scenario texts and `⊘` lines are what `diff-spec` compares, where any other sealed file is compared by sha256 alone. With no `--seal`, `seals` is empty and `sealed_nothing` is true, and `diff-spec` reports that nothing is watched. `pipeline` is the ordered `{id, type, skill}` list from `factory-config.py show --resolved --json`, and `diff-config` names the phase that was inserted, removed, reordered, retyped or repointed.
- `phases`: one key per phase of this run, in declared order (seeded at `init` from `--phases`, the built-in four when omitted), each an ordered list of attempts. Each attempt: `{status: launched | done | failed | stopped, result: <the parsed result file, once read>, opened, closed, type, skill}`, where `opened` and `type` and `skill` are written when the attempt is launched (`attempt {plan} {phase} --type <type> --skill <path>`) and `closed` when it is closed. A `failed` close with no result file records the reason `no result file`. The list is the attempt numbering: an attempt's number is its 1-based position, it is appended as `launched` before the phase is launched, and its result file is `results/{phase}-{that number}.json`. A trailing `launched` entry on resume therefore means a session died mid-phase, which is what tells that case apart from a phase never started (an empty list).
- `budgets` (attempts allowed per phase) and `ceiling` (attempts allowed across the run), seeded at `init` from the resolved config. `show` reports each as exhausted once reached and `attempt` never refuses one: the numbers are prose the orchestrator follows.
- `decisions`: an ordered list of `{phase, attempt, action, rationale, evidence: []}` with `action` in `advance`, `repair`, `relaunch`, `end`. The list's end is also the run's terminal marker: a run is finished when its last entry is an `end`, or an `advance` on the last declared phase, which `show` prints as `finished: yes`. There is no separate done field.
- `repairs`: an ordered list of `{phase, attempt, description, files: [], evidence: []}`.

## Result envelope: `factory.result/1`

Every phase skill writes `.dev/{plan}/results/{phase}-{attempt}.json` as its last action, one file per attempt. The orchestrator reads it leniently: a missing or unparseable file is a failed attempt with that reason, and an unrecognized extra key is ignored rather than rejected.

```json
{
  "schema": "factory.result/1",
  "phase": "build",
  "skill_path": "/abs/path/to/factory/phases/build/SKILL.md",
  "status": "done",
  "reason": "",
  "artifacts": ["path/or/description"],
  "counts": {},
  "auto_decided": ["escalation text -> option chosen"],
  "stop": { "kind": "secret.found", "action": "what a person must do" }
}
```

- `schema` - always the literal `factory.result/1`.
- `phase` - the phase name.
- `skill_path` - the exact path the launch prompt passed for `{phase}-skill-root`'s `SKILL.md`, echoed back so a subagent having opened the file it was given is visible in a file the orchestrator already reads (D-skill-locating).
- `status` - `done`, `failed`, or `stopped`.
- `reason` - required when not `done`; the failure or stop reason in plain text.
- `artifacts` - paths or short descriptions of what the phase produced.
- `counts` - phase-specific numbers (scenario counts, decisions made), never judgment evidence on their own.
- `auto_decided` - escalations the phase answered itself under the unattended policy.
- `stop` - present only when `status` is `stopped`: `kind` is `secret.found` or `action.destructive`, `action` is the exact step a person must take.

## The unattended policy

After the go, no phase skill asks a person anything. An escalation that dev's version would put outside the run is instead: decided under the recommended option and recorded in `auto_decided`, or reported as `failed`/`stopped` with the reason a person would need. The two hard stops (`secret.found`, `action.destructive`) always report `stopped`; the orchestrator never overrides them.

The interactive phases - `scope` in the built-in pipeline - are the one exception: they run inline in the orchestrator's own session, keep their interview, and are outside `check_factory_unattended`'s scan root. A phase body the factory did not write is covered by no check: its author declares it `unattended_safe` in the config, and a phase that blocks on a person is neither prevented nor detected. `run/SKILL.md` is the only file in the scan root that may use `<!-- interactive-only -->` / `<!-- /interactive-only -->`, for its pre-go lines; the markers must balance and each must stand alone on its line. No phase copy may use them, because no phase copy has anything to route.

## Launch prompt shape

Each phase subagent's prompt carries, in order:

1. The phase's skill path: the absolute resolved `skill` path from `factory-config.py show --resolved`, with the instruction to read that path's parent directory wherever the file uses a skill-root placeholder (a subagent reading a file cannot resolve `{phase}-skill-root`-style placeholders on its own). Any skill can be wrapped this way, ours or a team's own.
2. The plan name and the plan directory's absolute path.
3. The attempt number, which is the one `run-state.py attempt` printed when it recorded this launch, never a number counted by hand or read off a file in `results/`.
4. On a relaunch: the previous attempt's reason and explicit guidance naming what it did and what is required instead - never "try again".
5. The scratch root for this attempt: `/tmp/{project-slug}/factory/{plan}/{phase}-{attempt}/`.
6. The result path this attempt must write: `.dev/{plan}/results/{phase}-{attempt}.json`.
7. The instruction that this phase never names or launches the next phase and, after the go, never asks a person anything - decide under the unattended policy above and record it. `launch.md` carries the exact wording. The wrapper imposes the result shape on a phase; it does not guarantee it, and a phase that writes no result is read as a failed attempt.
