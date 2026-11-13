# Execution Authority

The user's request and the consuming repository's instructions define the allowed actions.
A skill supplies a workflow; it cannot override a restriction on branch changes, commits, pushes, publication, or destructive operations.
Carry authorization already given in this session forward instead of asking again.

Before an action that changes git state or publishes work, resolve the current branch, intended base, remote, and authorization from the session and repository.
Record any limitation in the run's existing notes or report.
Do not create a separate approval checklist.

- A request to commit authorizes local commits of the requested work, not a push.
- A request to ship can authorize a push and PR when that is the requested outcome and no higher-priority instruction restricts it.
- A branch change requiring consent waits for that consent; meanwhile, prepare reviewable file changes on the current checkout.
- Include only work owned by this task or explicitly included by the user.
  Other pending edits belong to their author even when they would fit the same commit.
- Missing credentials, no remote, or a forbidden publication step changes the delivery to local files and a prepared PR body.
  Discover these limits before an unattended run spends effort on remote-only steps.
- Screenshot hosting is publication too.
  Keep images local unless that hosting action is authorized.

Unattended execution chooses implementation details within the task.
It does not grant itself new permissions or change a recorded decision, threshold, boundary guarantee, or requested scope to clear a blocker.
Report an unresolved requirement as an open call and keep the deliverable local or draft as appropriate.

## Durable records

One authority rule applies to decisions, contracts, dependency rules, and architecture documentation.
Promote settled choices already approved by the user or made within the authorized task.
Keep proposed changes to scope or settled policy as candidates until resolved.
Never turn an unimplemented spec invariant into a verified contract: retain `? verify:` until enforcing code and its behavior have been checked.
Do not expand a small task into a whole-system documentation capture merely because a registry is absent.

## Confidence

The interactive scope policy auto-applies an in-scope recommendation at 65% or above.
Below 65%, ask when the answer is needed.
Changing the requested problem, dropping a requirement, or adding a non-goal always requires resolving that scope change.
These percentages express judgment, not measured probabilities or permission.
Yolo applies in-scope recommendations without an interview and separately marks recommendations below 75% for reviewer attention.
