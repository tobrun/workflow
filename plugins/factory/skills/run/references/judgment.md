# Judgment

Per phase, the evidence commands the orchestrator re-runs as proof and what a `done` looks like. `run-state.py check-result` gives the result file's own claim; these checks are the independent evidence read before trusting it.

## scope

`done`: `lint-spec.py .dev/{plan}/spec.md` reports clean, and the go was recorded (the interview happened inline in this session, so the orchestrator already knows).

## scope-review

`done`: `lint-spec.py` clean, and `spec-review_N.md` contains a line whose whole text is exactly `Verdict: APPROVED` - nothing after it - with a `Rounds:` line. Check it as a whole line, `grep -cx 'Verdict: APPROVED'`, never as a substring: a longer verdict such as `Verdict: APPROVED WITH DEFERRALS` is not a `done`. A `failed` result with reason `rescope` is not repaired: it ends the run naming what a future `scope` run must revisit.

## build

`done`: `check-tests.py .dev/{plan}` clean, the spec's Validation block green, commits since handoff match the change plan's numbering, and (when the spec has `[e2e]` scenarios) the e2e report's data block shows every scenario passed.

## ship

`done` has two endings, decided by what `origin` resolves to:

- **A GitHub remote**: `pr-evidence.py check` clean, `review_N.md` carries a verdict for HEAD, and `gh pr view` shows the PR open with head equal to HEAD.
- **A remote no GitHub host backs** (the fixture's bare origin): `gh pr create` cannot succeed, so ship cannot reach `done` at all. The evidence is `git ls-remote origin` showing the branch at HEAD and `pr.md` written to disk. The orchestrator ends the run with a report naming the pull request as the one unfinished step - this is the fixture's expected ending, not a factory fault, and is judged under the third `gh` fault category below, never as a park.

## The failure ladder

Cheapest sufficient step, recorded before it is acted on:

1. **Repair.** Fix it yourself and re-run the phase's checks - at most two repairs per attempt; the third fix is a relaunch and counts as a new attempt. A repair after ship's review moves HEAD, so the review no longer names HEAD and ship relaunches.
2. **Relaunch.** A fresh subagent, guidance naming what the previous attempt did and what is required instead - never "try again".
3. **End.** Three attempts of one phase without a `done` the orchestrator accepts: end the run with a report naming the last reason, the artifacts, and what a person could do.

Every repair is committed on the run branch and bound by the two hard stops: never rewrite history, force-push, or delete outside the run branch.

## Fault categories during ship

Three, told apart by the error text, never guessed:

1. **`gh` unauthenticated or GitHub unreachable** (token, credential helper, or sandbox network): an environment fault, the run ends naming the missing prerequisite.
2. **No GitHub remote behind `origin`**: `gh pr create` refuses with "none of the git remotes configured for this repository point to a known GitHub host" before any network call, even with `gh` installed and authenticated. Match this text first - its trailing `gh auth login` hint is never read as a credentials fault. The branch stays pushed, `pr.md` stays written, the run ends with a report naming the origin and the pull request as the one unfinished step.
3. **A secret found after build committed it**: the run ends without a pull request, the branch is left unpushed, the report names the commit to purge.
