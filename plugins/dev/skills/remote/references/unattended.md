# Unattended remote run

This session runs unattended in a sandbox workspace, dispatched by the operator's `dev:remote` skill.
Nobody watches it and nobody can answer a question; the operator reads the committed branch and the plan directory afterwards.

- This session runs one phase: the skill it was invoked with.
  End the session when that skill's wrap-up is done; the runner starts the next phase in a fresh session, so never begin it yourself, even where a skill or rule says the next step follows.
- Run the invoked skill as written, under the no-question contract and the hard stops of `{plugin}/skills/yolo/SKILL.md`.
  Read those two sections before the skill's first step, and apply their rules about questions and human calls; their rule about the next step is replaced by the line above.
  Where they mention the PR's Open calls, that is `.dev/{plan}/pr.md`.
- The plan is `.dev/{plan}/` on branch `{branch}`, and `origin/{default}` is the merge base the operator dispatched from.
  Stay on `{branch}`; never fetch, pull, rebase, or merge.
<!-- publish: sandbox -->
- Publication is authorized: git and `gh` in this workspace reach `origin` with credentials the operator allocated, and the dispatch is the operator's go-ahead to push `{branch}` and open its pull request.
  When the invoked skill is `ship`, in either mode,, run its pull-request phase as written, against `{default}`: push `{branch}`, publish evidence images with `pr-evidence.py publish`, create or update the pull request with the draft rules, and follow its required checks per the CI parity reference.
  `build` never pushes.
  Push only `{branch}` and the evidence branch, never force-push, and never change how git or `gh` authenticate (`gh auth`, credential helpers, remote URLs); a rejected push or an authentication failure is a blocker to record, not a problem to work around.
<!-- publish: laptop -->
- Publication is withheld: the operator publishes this run from their own machine.
  Never push, open or edit a pull request, publish evidence images, or try to authenticate `gh` or git.
  When the invoked skill is `ship`, in either mode,, it runs its default flow and its pull-request phase ends at the committed branch, a complete `pr.md` whose Evidence comes from `pr-evidence.py extract`, and a passing `pr-evidence.py check`; on the CI parity line, write `publication deferred to the operator` and list each check only the pull request can verify as remote-only.
<!-- publish: end -->
- Commit every change you keep on `{branch}` before the session ends.
  Only commits leave this workspace; uncommitted work is treated as abandoned.
- A desktop session is attached (`DISPLAY` is set), so headed browsers and screenshots work; the operator may be watching it, through a terminal there that follows this session.
  Leave that terminal open, and bring the window you are checking to the front before a screenshot.
- MCP servers in this session are relayed with the operator's own rights; use them read-only.
- When a problem cannot be resolved within these rules, record it where the stage records open calls, then end the session with a plain report as your final message.
