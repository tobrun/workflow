# Eval Results

Status: three functional runs recorded on 2026-09-25, before the skill's first release.

Every run executed the skill text directly in a Claude Code subagent (not through an installed plugin), on `git clone --local` scratch copies, with a simulated user who accepted every recommendation, skipped the closing newcomer question, and declined installs.

| Eval | Reference repository and commit | Host | Result | Notes |
| ---- | ------------------------------- | ---- | ------ | ----- |
| merge-prune | `~/ws/blog` at 0a55308, plus its untracked AGENTS.md | Claude Code (subagent) | pass (5/5 and every_eval) | stale `src/content/config.ts` updated, em dashes gone, test layers and ci recorded as open items, CLAUDE.md untouched and flagged, checker clean on the first iteration |
| rich-existing | `~/ws/mapcode` at 0eb9c91 | Claude Code (subagent) | pass (3/4; the re-run assertion was exercised on the blog copy below) | root-test rule kept, live e2e recorded as `e2e-env`, real scopes used; also caught `.dev/` not being gitignored and a stale models-generator claim |
| re-run (merge-prune output) | the blog copy after the first run | Claude Code (subagent) | pass | 70 of 72 lines kept verbatim; the 2 changes corrected an unconditional analytics claim that the code makes conditional |

## Fixes the runs drove

- The checker rejected `bun turbo` (a dependency's binary) as a missing script; `bun`, `yarn`, and implicit `pnpm` calls now fall back to declared dependencies and print a note.
- The checker now checks path filters on `test` subcommands, knows framework and asset extensions, notes unknown runners in Commands, and exempts naming conventions such as `kebab-case.mdx`.
- SKILL.md: the skill root is defined, installs of any kind need a question, a command whose dependencies are missing "cannot be run" rather than fails, and defects noticed while probing go to the closing report.
- References: scope fallback for short or non-conventional histories, e2e harnesses that drive source, configured retries as open items, conflicts limited to rules an agent file states, and approval tables grouped by section.
- A refresh starts from the existing file, the template's rule lines are exempt from pruning, and the approval table counts kept lines instead of listing them.
