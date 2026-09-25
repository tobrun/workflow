---
name: agents-md
description: Probe a repository's stack (manifests, CI, test layout, git history), map it onto the dev workflow's SDLC concepts (test layers, mocking at boundaries, CI parity, fix-at-source quality, commit and PR conventions), and write or refresh its root AGENTS.md with the repo's exact commands, merging an existing AGENTS.md or CLAUDE.md and recording gaps as open items. Use when preparing a repository for agent work, or when its AGENTS.md has drifted from the code.
---

# AGENTS.md

The deliverable is one file: the repository's root `AGENTS.md`.
It encodes the lessons of the dev workflow as short outcome rules any agent can follow without that workflow installed, filled in with this repo's real commands, layout, and gaps.

Limits:
- Write only the root `AGENTS.md`; never write CLAUDE.md, CLAUDE.local.md, nested agent files, docs, tests, CI, configs, or `.gitignore`.
- A missing concept (no integration layer, no mocked e2e environment, no merge gate) becomes an open item, never a scaffold.
- Never commit, and never invoke another skill; recommend the next step instead.

`{agents-md-skill-root}` below is the directory holding this SKILL.md, and `{project-slug}` is the basename of the repository root.

## 1. Probe

Follow [references/probe.md](references/probe.md), read-only.
In a workspace or large repository, launch one background read-only subagent per probe group and merge their rows.
Collect every fact into one table shown to the user: `concept | value | evidence | confidence`, where evidence is a file and line, or a command and its output.

## 2. Map

Resolve every slug in [references/concepts.md](references/concepts.md) to **present**, **absent**, or **unresolved**, using its "Reading the probe onto a row" rules.
Absent rows take their "When absent" behavior without a question.
Read the existing root AGENTS.md and CLAUDE.md now and classify each line per [references/merge.md](references/merge.md); every **conflict** joins the unresolved list.

## 3. Interview

Ask only about unresolved rows and conflicts, one question at a time, using the host's structured user-input tool when available.
Each question carries a recommendation and `Confidence: NN%` with the one fact that would most change it.
At `Confidence: 75%` or above, apply the recommendation instead of asking, and list every auto-applied item once at the end of the interview so one reply can overturn any of them.
When several remain, ask in this order: the merge gate, the e2e launch command and environment, conflicts with the lessons, then the default branch.
Close with one skippable question: what do newcomers, human or agent, get wrong here that the code does not show?
Its answer is the most valuable content in the file; place each gotcha in the section it concerns.
Never guess a command: ask, or record `none` with an open item.

## 4. Verify the commands

Run each command bound for the Commands section once, in the background with a timeout, while drafting.
Ask before any install, local or global (the Setup command included), and before anything that deploys, publishes, needs credentials, or would run for more than about five minutes.
Keep runners from installing on their own when dependencies are missing (for example `bun --no-install`).
A command whose dependencies are not installed, or that was declined, cannot be run: it stays in the file with its probe evidence, which the approval table shows.
Only a command that ran and failed gets an open item quoting its first error line.

## 5. Draft and merge

Create `/tmp/{project-slug}/bootstrap/` and draft `AGENTS.md` there from [references/template.md](references/template.md), folding in the kept and updated lines from step 2.
Keep the rule lines' meaning intact and word them in the repo's vocabulary; the file is read by every future session, so every line must earn its place.
A refresh, where the existing AGENTS.md already passes the checker's structure, starts the draft from that file instead of the template, changes only lines whose facts changed, and repeats the install and newcomer questions only when a Commands slot or a gotcha is in play.

## 6. Check loop

Loop until it exits 0:

```bash
python3 {agents-md-skill-root}/scripts/check-agents-md.py /tmp/{project-slug}/bootstrap/AGENTS.md --root .
```

Fix the draft, never the checker or the repository.
Resolve each `note:` line by running the command; a command that cannot be run (step 4) keeps its probe evidence instead.

## 7. Approve and write

Show the diff, `git diff --no-index` of the current AGENTS.md (or `/dev/null`) against the draft, then one table of updated and pruned lines with their evidence (kept lines as a count per section), then the open items.
Wait for approval; apply requested edits and re-run step 6.
Copy the draft to the repository root and run the checker once more against `AGENTS.md`.

Close with:
- The open items, each with what closing it takes.
- Defects noticed while probing that fit no section or slug, marked unverified.
- When a CLAUDE.md exists, a recommendation to delete it, or trim it to Claude-only content such as `@` imports, since its content now lives in AGENTS.md.
- The next step: review and commit the file (`/dev:commit` if the dev plugin is installed); nothing is committed here.
