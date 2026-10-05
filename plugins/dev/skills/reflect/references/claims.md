# Claims

The memory store, the claim schema, and the judgment `claims.py` cannot make for you.

## The store

`$DEV_MEMORY_DIR`, default `~/.dev-workflow/memory/`:

| path | written by | role |
| ---- | ---------- | ---- |
| `journal/YYYY-MM-DD.jsonl` | `skill-metrics.py end` | one entry per run: measured signals, checker runs, narrated friction |
| `claims.jsonl` | `claims.py add`, `retract` | every claim ever accepted, with its status |
| `consolidated.jsonl` | `claims.py mark` | runs already read |
| `resolutions.jsonl` | `claims.py resolve` | thread, fixing commit, eval id, time |
| `skills/{skill}.md`, `threads.yaml`, `progression.md` | `claims.py rebuild` | derived; every line cites a claim id |

Records are appended or change status; they are never deleted.
A retracted claim stays so the same evidence cannot quietly bring it back.

## A journal entry

The fields an extracting agent reads:

- `id`, `skill`, `repo`, `started_at`, `transcript`, `anchor_line`.
- `signals`: up to 20 per type, each `{type, transcript, line, excerpt}`. Types: `interrupt` (the user stopped the run), `denied` (the user refused a tool call), `tool_error` (a non-checker tool failed), `user_turn` (anything the user typed after invocation).
- `checkers`: per checker script, `runs`, `failed`, and `first_failure` with its line.
- `counts`: the skill's own measured counters, `signal_counts`: totals before the cap.
- `friction`: up to three lines the skill narrated about itself. Narrated, not measured: a claim resting on one alone takes salience 1 unless a measured signal in the same run agrees.

## Candidate shape

One JSON object per line:

```json
{"skill": "build", "thread": "build-validation-reruns", "kind": "friction",
 "claim": "Build reran the full Validation block after each change set; the user stopped it.",
 "quote": "stop rerunning the whole validation block",
 "source": {"run": "r-20261005T100000-build", "line": 412},
 "salience": 2}
```

- `skill`: the skill whose instructions the claim is about, which need not be the run's skill (a build run can expose a scope defect).
- `thread`: `{skill}-{problem}` in lowercase slug form; reuse an existing slug for the same problem.
- `kind`: `friction` (the skill made the run slower or the user intervene), `defect` (the skill's instructions were wrong or contradictory), `cost` (a measured cost far above that skill's median), `success` (a skill rule that demonstrably saved the run; keep these, they say what not to remove).
- `claim`: one sentence, at most 240 characters, no em dash.
- `quote`: verbatim text, 8 to 300 characters, found at the source. Whitespace and case are ignored, nothing else is.
- `source`: `run` plus `line` (a line of the run's transcript, or with `transcript`, of one of its subagent transcripts); omit `line` to quote the journal entry itself.
- `salience`: 1 noted once, 2 cost real time or a user intervention, 3 broke the run or produced a wrong result.
- `supersedes`: optional id of an active claim this one replaces with better evidence or a sharper reading.

## What is not a claim

- A `user_turn` that answers a question the skill was designed to ask, such as a scope interview answer.
- A checker that failed and passed on the next run: that is the loop working.
- A `denied` call that reflects the user's standing policy rather than a mistake the skill made.
- Anything about the consuming repository's code quality, its tests, or its people.
- A guess about why without a quote that shows it; write the observable claim instead.
