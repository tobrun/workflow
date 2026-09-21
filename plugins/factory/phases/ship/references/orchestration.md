# Orchestration

The review always uses independent agents in two batches. Select the transport
without changing the panel:

Both transports use the same scratch root, such as `/tmp/ship-{session-id}/`,
with a `results/` directory per batch. Result files in that directory are the
only channel for agent output. A batch in flight means the run is not over:
never end your turn while launched agents are still outstanding - wait for the
host's completion signal, read the results, and continue. Never retrieve results by messaging an agent,
waiting for relayed replies, or re-spawning an agent that already finished;
a fresh spawn has none of the original context and its output must not be used.

1. **Native transport (preferred):** when the host provides a multi-agent
   facility, launch all agents in a batch in one native parallel call. This is
   the existing Claude Code, Codex, and opencode behavior; do not route those
   hosts through subprocesses.
2. **Pi transport:** when `PI_CODING_AGENT=true` and `pi` is available, write
   one prompt file per agent and run
   [scripts/run-pi-agents.sh](../scripts/run-pi-agents.sh). The runner launches
   one isolated `pi --print` process per prompt concurrently and inherits the
   current provider, model, and reasoning level.
3. **Unavailable:** if neither transport exists, stop. The orchestrator must
   not impersonate the panel or review the code itself.

No dynamic workflows are involved. The Workflow-tool variant in
[orchestration-heavy.md](orchestration-heavy.md) belongs to the interactive
dev flow only; an unattended phase never reads it.

## Native transport

Use the host's plain subagent tool exactly as provided (the Agent tool on
Claude Code, `spawn_agent` on Codex, the `task` tool on opencode). Launch all
agents of a batch in a single parallel call: on opencode that means issuing
every `task` call of the batch in one message with the default general
subagent. Native subagents receive the prompt contracts below directly.

Result delivery is file-based, because on some hosts subagents run in the
background and their final message never reaches the orchestrator:

1. Before launching a batch, create `{scratch-root}/{batch}/results/`.
2. Each agent's prompt instructs it to write its JSON report to
   `{scratch-root}/{batch}/results/{safe-agent-name}.json` as its last action,
   then end with a one-line confirmation naming that path. The result file is
   the deliverable; the final message is not.
3. When the host signals that the batch's agents have finished, read the
   result files.
4. A missing or unparseable file after one re-read marks only that agent as
   failed, same as a crashed agent.

Agents stay read-only in the repository; their only write is their own result
file under the scratch root.

## Pi transport

Use the shared scratch root. For each batch:

1. Create `prompts/` and `results/` directories dedicated to that batch.
2. Write one `{safe-agent-name}.md` prompt per agent. Include the complete
   prompt contract from the relevant batch below. Tell the process to inspect
   the repository but never modify it.
3. From the repository root, run:

   ```bash
   bash {ship-skill-root}/scripts/run-pi-agents.sh \
     {batch-root}/prompts \
     {batch-root}/results
   ```

4. Read each `{safe-agent-name}.out` as that agent's final response. A matching
   `.err` or missing/unparseable `.out` marks only that agent as failed.
5. Keep the scratch files until aggregation is complete so malformed output is
   auditable, then remove them.

The runner disables child skills, extensions, prompt templates, and sessions
to prevent recursion and state leakage. It retains project context files and
read-oriented tools (`read`, `bash`, `grep`, `find`, `ls`) so each process can
inspect the diff, surrounding code, and tests independently. Do not add
`write` or `edit`.

## Batch 1: lens agents

Launch one agent per selected lens, all in a single message so they run concurrently.
Each lens prompt is assembled from:

1. A role line: `You are the "{key}" voice on a code-review panel; other lenses cover other concerns.`
2. The lens definition plus the shared rules from `lenses.md`.
3. The brief, and the spec context when a spec was found.
4. The diff location: `Read the full diff from {diffFile}; the repo working tree holds the post-diff state. Read repo files whenever the diff alone is not enough to judge.`
5. The output contract below.

Also state: `Do not modify repository files. This is a read-only review.`
On the native transport, add the result-file instruction from the transport
section; on Pi, the runner captures the final message instead, which must be
exactly one fenced JSON block.

Output contract (the JSON every lens agent must produce):

```json
{
  "verdict": "PASS | CONCERNS | BLOCK",
  "findings": [
    {
      "severity": "BLOCK | CONCERN | NIT",
      "file": "path/relative/to/repo",
      "line": 1,
      "title": "one line",
      "detail": "2-4 lines naming the evidence",
      "scenario": "what triggers it (required for BLOCK)"
    }
  ],
  "good": ["up to 5 bullets"]
}
```

If an agent errors or returns something unparseable, record it as a failed lens and continue.
Never retry a failed lens more than once.

## Batch 2: verifiers

`scripts/aggregate-findings.py plan` collects every BLOCK and CONCERN from the
lens results and numbers them (`f1`, `f2`, ...) so verdicts map back. NITs skip
verification. Duplicates are not merged yet: two lenses that flagged the same
line get refuted independently, and the merge happens after their verdicts.
Launch one verifier per finding, again all in a single message.
If that would exceed 10 verifiers, group the findings by file into at most 10
shards and give each verifier its shard; each finding is still verified
independently, one verdict object per finding.
Verifier prompt:

```
Adversarially verify each of these code-review findings, reported by the
named lens. Your job is to REFUTE them.
{findings as JSON, each with its id}
From the diff at {diffFile}, read the hunks for your findings' files (search
by path - do not ingest the whole diff), plus the actual files in the repo.
Check whether each claimed problem is real: does the scenario actually
trigger, is the claim accurate against the files, does something else
already handle it?
Produce exactly one JSON array with one object per finding:
[{"id": "f1", "status": "CONFIRMED | PLAUSIBLE | REFUTED", "reason": "..."}]
CONFIRMED = you reproduced the reasoning against real files and it holds.
PLAUSIBLE = you could not refute it, but could not fully confirm it either.
REFUTED = wrong, unreachable, or already handled; say exactly why.
```

Delivery follows the transport: on native, the verifier writes the array to
its result file; on Pi, it replies with the array as one fenced JSON block.

Aggregating the verified results is `aggregate-findings.py aggregate` per the skill's Aggregate step - never another agent.
