# Factory Run Protocol

Owned by the `run` skill. Every phase copy (`scope`, `scope-review`, `build`, `ship`) reads this file for the result envelope and the unattended policy; `run/SKILL.md` also reads it for the run state schema and the launch prompt shape.

## Run state

`.dev/{plan}/factory-run.json`, written only by the `run` skill, via `run-state.py`. It exists from `init` on, before any branch. Fields:

- `plan`, `request`, `base` (the default branch at `init`).
- `branch`, `spec_sha256`, `scenario_texts` (per change set), `not_doing_lines` (the `⊘` lines), `dirty_files` - all written at `handoff`.
- `phases`: `{scope, scope-review, build, ship}`, each an ordered list of attempts. Each attempt: `{status: launched | done | failed | stopped, result: <the parsed result file, once read>}`.
- `decisions`: an ordered list of `{phase, attempt, action, rationale, evidence: []}` with `action` in `advance`, `repair`, `relaunch`, `end`.
- `repairs`: an ordered list of `{phase, attempt, description, files: [], evidence: []}`.

## Result envelope: `factory.result/1`

Every phase skill writes `.dev/{plan}/results/{phase}-{attempt}.json` as its last action, one file per attempt. The orchestrator reads it leniently: a missing or unparseable file is a failed attempt with that reason, and an unrecognized extra key is ignored rather than rejected.

```json
{
  "schema": "factory.result/1",
  "phase": "build",
  "skill_path": "/abs/path/to/factory/skills/build/SKILL.md",
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

After the go, no phase skill asks a person anything. An escalation that dev's version would ask a human about is instead: decided under the recommended option and recorded in `auto_decided`, or reported as `failed`/`stopped` with the reason a person would need. The two hard stops (`secret.found`, `action.destructive`) always report `stopped`; the orchestrator never overrides them.

`scope` is the one exception: it runs inline in the orchestrator's own session, keeps its interview, and is outside `check_factory_unattended`'s scan root. Every other phase copy, and `run/SKILL.md`'s own person-routing lines, live inside `<!-- interactive-only -->` / `<!-- /interactive-only -->` blocks or outside the scan root entirely.

## Launch prompt shape

Each phase subagent's prompt carries, in order:

1. The phase's skill path: the absolute path to `factory/skills/{phase}/SKILL.md`, with the instruction to read `{phase}-skill-root` as that path's parent directory (a subagent reading a file cannot resolve `{phase}-skill-root}`-style placeholders on its own).
2. The plan name and the plan directory's absolute path.
3. The attempt number.
4. On a relaunch: the previous attempt's reason and explicit guidance naming what it did and what is required instead - never "try again".
5. The scratch root for this attempt: `/tmp/{project-slug}/factory/{plan}/{phase}-{attempt}/`.
6. The result path this attempt must write: `.dev/{plan}/results/{phase}-{attempt}.json`.
7. The instruction that this phase never launches another phase and, after the go, never asks a person anything - decide under the unattended policy above and record it.
