# Pipeline config

`.factory/config.yaml` in the consuming repository declares the pipeline the orchestrator drives.
Its absence is not an error: the built-in default pipeline applies, our four phases run from the installed plugin, and nothing is written to the repo.

## Schema

```yaml
version: 1
defaults:
  ceiling: 12
  attempts: 3
types:
  <type-name>:
    interactive: true
    requires: "prose the orchestrator judges by running a tool itself"
    checks:
      - - lint-spec.py
        - ${plan_dir}/spec.md
    seals:
      - ${plan_dir}/spec.md
    attempts: 3
phases:
  - id: <phase-id>
    type: <type-name>
    skill: ${plugin_root}/phases/<phase-id>/SKILL.md
    checks:
      - - check-tests.py
        - ${plan_dir}
    unattended_safe: true
```

`version` is always `1`. `defaults`, `types` and `phases` are the schema's only other top-level keys; an unknown key is a `check` finding.

### Types: exactly five axes, no more

- `interactive` (bool) - runs inline in the orchestrator's own session rather than launched.
- `requires` (prose) - the outcome contract the orchestrator judges when the type's `checks` don't cover it, the same way `judgment.md` reads a phase today.
- `checks` (a list of argv lists) - commands that verify the outcome. Each entry is an argv list, never a shell string.
- `seals` (a list of paths) - artifacts whose drift `run-state.py handoff`/`diff-spec` watches. May use `${plan_dir}`.
- `attempts` (int) - the per-type default retry budget.

The type set is open: any name declared under `types` is usable by a phase. A type the orchestrator has never seen is judged by its declared axes alone.

### Phases: an ordered list

Each entry is `{id, type, skill, checks, unattended_safe}`. `id`, `type` and `skill` are required; `checks` and `unattended_safe` are optional per-phase overrides.

- `checks`, when present on a phase, **replaces** the type's `checks` entirely - it never merges.
- `unattended_safe: true` is required for a foreign phase (see Phase classes below); built-in and injected phases don't need it.

`defaults` and `types` are reserved words: no phase may take either as its `id`, since the key-path grammar below roots at both.

### Placeholders

A `skill` path and a check's argv elements may use `${plugin_root}`, `${repo_root}`, `${plan_dir}`, `${phase}` and `${attempt}`. A check's **executable** element (argv[0]) may only use `${plugin_root}` or `${repo_root}` - `${plan_dir}` and the others can't be allowlisted before a run exists. `show --resolved` leaves every placeholder literal; the orchestrator substitutes them when it launches or runs a check.

### Phase classes

Three classes, by path and provenance, established by `factory-config.py check`:

- **built-in** - the skill path starts with `${plugin_root}`.
- **injected** - the skill sits under `.factory/skills/` and its current sha256 matches the entry `.factory/.inject.json` recorded for it.
- **foreign** - anything else, including an injected copy whose hash has drifted from its manifest entry.

Only a foreign phase needs `unattended_safe: true`; the orchestrator never hand-repairs a foreign phase's failure, only relaunches it or ends the run.

## The key-path grammar

`set` and `unset` address one value at a time by a dotted path:

- `<phase-id>.<key>` - a key on an existing phase entry. The phase must already exist; `set` never creates a phase implicitly.
- `defaults.<key>` - a key under `defaults`, created if absent.
- `types.<name>.<axis>` - an axis of a type, created if absent.

A list-valued key takes repeated `--item` flags in place of a positional value. `unset` removes the named key; a phase left with no keys at all is dropped from `phases` entirely.

## The manifest `check` reads

`.factory/.inject.json`, written by `inject`, at the shape:

```json
{"plugin": "<name>", "version": "<semver>", "files": {"<path relative to .factory/>": "<sha256>"}}
```

## The built-in default pipeline

Equal to today's four phases: `scope` (interview, interactive), `scope-review` (review), `build` (implement), `ship` (ship), each declared with `${plugin_root}` so a plugin move only touches this one reference and `factory/scripts/factory-config.py`'s embedded copy. Today's criteria partition across the allowlist: `lint-spec.py ${plan_dir}/spec.md` (scope), `check-tests.py ${plan_dir}` (build) and `pr-evidence.py check ${plan_dir}/pr.md` (ship) become `checks` entries, each executable written as a `${plugin_root}/phases/{phase}/scripts/...` path; `gh pr view`, `git ls-remote` and the `Verdict: APPROVED` grep stay `requires` prose the orchestrator judges itself. `scope`'s `interview` type seals `${plan_dir}/spec.md`. Every built-in type declares `attempts: 3`; the default declares a ceiling of 12 attempts per run.
