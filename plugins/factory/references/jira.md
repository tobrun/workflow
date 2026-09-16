# Jira mirror reference

This reference is used only when the consuming repository opts into Jira in
`.dev/config.json`. The `.dev/` files remain the source of truth; Jira mirrors
the records identified by the keys persisted in those files.

## Configuration and activation

Read `.dev/config.json` before doing any Jira work. The supported shape is:

```json
{
  "jira": {
    "enabled": true,
    "site": "acme.atlassian.net",
    "project": "PROJ"
  }
}
```

`jira.project` is required when enabled. `jira.site` is optional and should be
passed to `acli` when present. If the file is absent, malformed, Jira is
missing, or `jira.enabled` is false, the workflow is pure-local. In the
disabled case do not mention Jira, ask a Jira question, or invoke `acli`.

When enabled, check that `acli` is installed and authenticated before the first
operation. A useful check is:

```text
acli jira auth status [--site SITE]
```

If the check fails, follow the failure protocol below: it needs credentials, so
an unattended stage parks the run.

## Command shapes

The exact flags may vary slightly with the installed `acli` release. Preserve
the intent and verify every created issue with `workitem view`. Treat a
non-zero exit, invalid JSON, missing key, or ambiguous result as a failure.

List open Initiatives for the configured project:

```text
acli jira workitem search --project PROJ --type Initiative --status open --json [--site SITE]
```

<!-- interactive-only -->
Only interactive `scope` selects the Initiative: ask the user to choose one returned Initiative.
<!-- /interactive-only -->
Unattended stages never select one; they use the Epic key already persisted in
`spec.md`. If none is returned, stop with a clear message that an existing open
Initiative is required; never create one.

Create the spec Epic linked to the chosen Initiative, then verify it:

```text
acli jira workitem create --project PROJ --type Epic --summary SUMMARY --parent INITIATIVE-1 --json [--site SITE]
acli jira workitem view EPIC-1 --json [--site SITE]
```

Create a task issue for a change set under the Epic, then verify it:

```text
acli jira workitem create --project PROJ --type Task --summary SUMMARY --parent EPIC-1 --json [--site SITE]
acli jira workitem view TASK-1 --json [--site SITE]
```

Discover the project's available transitions once per issue type per run, from
a representative issue, and reuse the names; re-discover only after a failed or
ambiguous transition. Match the displayed names exactly and require one
unambiguous transition for the requested state:

```text
acli jira workitem transition-list --issue EPIC-1 --json [--site SITE]
acli jira workitem transition --issue EPIC-1 --transition "In Progress" --json [--site SITE]
```

Use the same discovery and transition sequence for task issues.

When a change set is superseded by a spec revision, transition the old issue
to its available closed state and add the required comment:

```text
acli jira workitem transition-list --issue TASK-1 --json [--site SITE]
acli jira workitem transition --issue TASK-1 --transition "Done" --json [--site SITE]
acli jira workitem comment --issue TASK-1 --body "superseded by change set 3" --json [--site SITE]
```

## Persistence and timing

After the Epic is created, write `Jira: PROJ-1` directly under the spec title
in `spec.md`. After each task issue is created, write its key directly under
the matching change set in the spec's change plan.

`scope` lists Initiatives during its interview and creates the Epic only
after the change plan is final. It then creates one Task per change set; a
re-run creates Tasks only for change sets that don't carry a key yet. On
supersede, close and comment the old issue before creating and persisting the
replacement issue.

build transitions the persisted Epic to In Progress at the start. The
orchestrator transitions each persisted change-set issue to In Progress
immediately when its wave is dispatched, and to Done only after the change set
is verified and its commit lands.

## Failure protocol

Jira is first-class when enabled. Any failed auth check, command, JSON parse,
verification, missing key, missing Initiative, unknown transition, or
ambiguous transition stops the skill immediately.
<!-- interactive-only -->
In interactive `scope`, explain the failed operation and ask the user how to proceed.
<!-- /interactive-only -->
In an unattended factory stage, an auth or permission failure parks the run
with an `environment.missing_credentials` condition; any other failure is retried
once, then ends the stage `blocked`, without a condition, with the failed
operation as its reason. Never silently skip Jira, continue
with divergent local-only state, invent an issue key, or mark a transition
successful without verification.

The Epic is not polled or closed by the skills. When build creates the work
branch and when ship opens the PR, include the Epic key in the branch name and
at the start of the PR title. A documented Jira automation rule closes the Epic
after the linked PR is merged.
