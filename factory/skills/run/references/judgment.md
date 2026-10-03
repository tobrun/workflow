# Judgment

How the orchestrator decides a phase is done, from what its type declares. `run-state.py check-result` gives the result file's own claim; the type's `checks` and `requires` are the independent evidence read before trusting it. Read them from `factory-config.py show --resolved`.

## Reading a type

- **`checks`**: each entry is an argv list. Substitute `${plan_dir}` (`.dev/{plan}`), `${phase}` and `${attempt}` in each element, then run the list element by element, never composed into a shell string. `check` already refused any executable element carrying `${plan_dir}`, `${phase}` or `${attempt}`, so only the arguments vary. An entry that exits non-zero is a failed check whose output is the reason.
- **`requires`**: the outcome contract in prose. Judge each criterion it names by running the tool it names yourself (`git`, `gh`, `grep`), as the built-in types below do. A type declaring no `checks` is judged on its result envelope and its `requires` alone; that is weaker evidence, so weigh it as such.
- **`attempts`** and the run's `ceiling`: the budgets the failure ladder follows.
- A phase never reaches `done` because its own result says so. Its result claims; the checks and `requires` decide.

## The built-in types

`interview` (the `scope` phase): `done` when `lint-spec.py` reports the spec clean and the go was recorded (the interview happened inline in this session, so the orchestrator already knows).

`review` (`scope-review`): `done` when `spec-review_N.md` contains a line whose whole text is exactly `Verdict: APPROVED` - nothing after it - with a `Rounds:` line, and the spec still lints clean. Check it as a whole line, `grep -cx 'Verdict: APPROVED'`, never as a substring: a longer verdict such as `Verdict: APPROVED WITH DEFERRALS` is not a `done`. A `failed` result with reason `rescope` is not repaired: it ends the run naming what a future `scope` run must revisit.

`implement` (`build`): `done` when `check-tests.py` reports clean, the spec's Validation block is green, commits since handoff match the change plan's numbering, and (when the spec has `[e2e]` scenarios) the e2e report's data block shows every scenario passed. Only `check-tests.py` is a declared check; the rest is `requires`.

`ship` (`ship`) has two endings, decided by what `origin` resolves to:

- **A GitHub remote**: `pr-evidence.py check` clean, `review_N.md` carries a verdict for HEAD, and `gh pr view` shows the PR open with head equal to HEAD.
- **A remote no GitHub host backs** (the fixture's bare origin): `gh pr create` cannot succeed, so ship cannot reach `done` at all. The evidence is `git ls-remote origin` showing the branch at HEAD and `pr.md` written to disk. The orchestrator ends the run with a report naming the pull request as the one unfinished step - this is the fixture's expected ending, not a factory fault, and is judged under the third `gh` fault category below, never as a park.

## The failure ladder

Cheapest sufficient step, recorded before it is acted on:

1. **Repair.** Only for a built-in phase or an injected copy that still matches its manifest - what `show --resolved` prints as class `built-in` or `injected`. Fix it yourself and re-run the phase's checks - at most two repairs per attempt; the third fix is a relaunch and counts as a new attempt. A repair after ship's review moves HEAD, so the review no longer names HEAD and ship relaunches. A foreign phase is never repaired: you never infer the meaning of an artifact you did not define, so a foreign failure goes straight to relaunch or end.
2. **Relaunch.** A fresh subagent, guidance naming what the previous attempt did and what is required instead - never "try again".
3. **End.** The phase's attempts reach its `attempts` budget (three for every built-in type) without a `done` the orchestrator accepts, or the run's attempts reach its `ceiling`: end the run with a report naming every phase's attempts, the last reason, the artifacts, and what a person could do.

Every repair is committed on the run branch and bound by the two hard stops: never rewrite history, force-push, or delete outside the run branch.

## Fault categories during ship

Three, told apart by the error text, never guessed:

1. **`gh` unauthenticated or GitHub unreachable** (token, credential helper, or sandbox network): an environment fault, the run ends naming the missing prerequisite.
2. **No GitHub remote behind `origin`**: `gh pr create` refuses with "none of the git remotes configured for this repository point to a known GitHub host" before any network call, even with `gh` installed and authenticated. Match this text first - its trailing `gh auth login` hint is never read as a credentials fault. The branch stays pushed, `pr.md` stays written, the run ends with a report naming the origin and the pull request as the one unfinished step.
3. **A secret found after build committed it**: the run ends without a pull request, the branch is left unpushed, the report names the commit to purge.
