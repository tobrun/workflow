# Unattended remote run

This session runs unattended on a fleet instance, started by the operator's `dev:remote` skill.
Nobody watches it and nobody can answer a question; the operator reads the pull request, the branch, and the plan directory afterwards.

- The run is a yolo run: `{plugin}/skills/yolo/SKILL.md` is its contract, with its no-question rules, its hard stops, and its wrap-up.
  The sibling skills it names are under `{plugin}/skills/`.
- The run is finished when every change is committed on the work branch, the branch is pushed, and its pull request is open at this checkout's HEAD - as a draft when an open call or a blocker remains, as yolo says.
  A runner checks exactly that each time this session ends, and resumes the session with what is missing until it holds.
  So never end on a plan, a question, a summary of next steps, or work left for later: carry it through, and record what cannot be resolved as an open call in the pull request.
- Work on `{branch}`, which the runner checked out; `origin/{default}` is the merge base the operator sent from.
  Commit as the work goes: only commits and the plan directory under `.dev/` leave this instance.
- Publication is authorized: git and `gh` on this instance reach `origin` with credentials the operator allocated, and starting this run is the operator's go-ahead to push `{branch}`, publish evidence images, and open its pull request against `{default}`.
  Push only `{branch}` and the evidence branch, never force-push, and never change how git or `gh` authenticate (`gh auth`, credential helpers, remote URLs).
- A hard stop ends the run as blocked instead: write the reason, in a few plain lines, to `{run}/blocked.md`, then end the session.
  A push or `gh` call that is rejected for lack of access is such a stop, after everything is committed and `pr.md` is complete, so the operator can publish from their own machine.
  A tool this instance lacks is not: do the rest of the work, and record what could not be run as an open call.
- A desktop session is attached (`DISPLAY` is set), so headed browsers and screenshots work; the operator may be watching it, through a terminal there that follows this session.
  Leave that terminal open, and bring the window you are checking to the front before a screenshot.
- MCP servers in this session are relayed with the operator's own rights; use them read-only.
