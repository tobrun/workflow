# Run Journal

`skill-metrics.py end` appends one entry per run to the cross-repository journal under `~/.dev-workflow/memory/journal/` (or `$DEV_MEMORY_DIR`), which the `reflect` skill consolidates into cited claims about the skills themselves.
The script measures the friction signals on its own from the transcript: interrupts, denied and failed tool calls, the user's own turns, and how often each checker ran and failed.

The one thing it cannot measure is where the run fought the skill's own instructions, so the skill adds that, as up to three `--friction "..."` arguments on the `end` call:

- One line each: the step, what the instruction asked, and what the run had to do instead, quoting the moment when there is one.
- About the skill's text, never the repository's code: "step 3 assumes one Validation block; this repo has two, so the wave gate ran neither" is friction, "the tests are slow" is not.
- None when the run went as the skill describes; an empty journal entry is the common and good case.

Set `DEV_MEMORY_DIR=off`, or `"memory": {"enabled": false}` in `.dev/config.json`, to keep a repository's runs out of the journal.
