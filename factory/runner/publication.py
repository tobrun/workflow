"""Runner-owned publication: push, open, and update the pull request when only publication is missing.

When ship's analysis is already valid for the current revision (a complete review that is
not BLOCK, a gauntlet record, and a `pr.md` that passes the evidence check), the remaining
steps are deterministic. The runner performs them itself with bounded retries instead of
spending another paid model attempt on a transient `gh` or network failure:

- push the run branch when the remote branch is missing or strictly behind;
- open the pull request when none is open, reconciling with the remote after any failed or
  interrupted request so a lost response never creates a second one;
- update the published body when its Evidence differs from `pr.md`.

Each operation has a stable id (run, kind, revision, body hash) and records its tries, so
deterministic retries are counted apart from the paid retry budget.
"""

from __future__ import annotations

import re
import sys
import time

from runner import faults, records
from runner import worktree as wt

TRIES = 3


def prerequisites(ctx) -> tuple[bool, str]:
    from runner import gates
    head = wt.head(ctx.worktree)
    index, review = gates.highest_index(ctx.plan_dir, "review")
    if review is None:
        return False, "no review yet"
    try:
        record = records.load_review(review.with_suffix(".json"), required_lenses=gates.SHIP_MANDATORY_LENSES)
    except records.RecordError as error:
        return False, f"review record not valid: {error.code}"
    if record.get("revision") != head or record.get("verdict") not in ("PASS", "CONCERNS"):
        return False, "review does not approve the current revision"
    gauntlet = ctx.plan_dir / "gauntlet.json"
    try:
        if records.load_gauntlet(gauntlet)["revision"] != head:
            return False, "gauntlet record is for another revision"
    except records.RecordError as error:
        return False, f"gauntlet record not valid: {error.code}"
    body = ctx.plan_dir / "pr.md"
    if not body.is_file():
        return False, "no pr.md"
    code, output, _ = gates.run_script(ctx, [sys.executable, str(gates.PR_EVIDENCE), "check", str(body)],
                                       "pr-evidence-check")
    if code != 0:
        return False, "pr.md fails the evidence check"
    return True, head


def attempt(ctx, operations: list[dict], kind: str, head: str, body_sha: str | None, action) -> tuple[bool, str]:
    """Run one deterministic operation with bounded backoff; `action` returns (done, detail)."""
    identity = records.canonical_sha256([ctx.plan, kind, head, body_sha])[:16]
    record = {"id": identity, "kind": kind, "revision": head, "tries": 0, "result": "failed", "details": []}
    operations.append(record)
    for index in range(TRIES):
        if ctx.stop_reason():
            record["details"].append(ctx.stop_reason())
            return False, ctx.stop_reason()
        record["tries"] += 1
        done, detail = action()
        record["details"].append(detail)
        if done:
            record["result"] = "done"
            return True, detail
        wait = min(2 ** index, 30)
        if ctx.deadline is not None and time.time() + wait > ctx.deadline:
            break
        if index + 1 < TRIES:
            ctx.sleep(wait)
    return False, record["details"][-1] if record["details"] else "not attempted"


def remote_state(ctx, head: str) -> str:
    """Where the remote run branch stands against `head`: absent, behind, synchronized, diverged, or unknown."""
    from runner import gates
    fetch = gates.execute(ctx, [gates.binary("git"), "-C", str(ctx.worktree), "fetch", "--quiet", ctx.remote,
                                f"+refs/heads/{ctx.branch}:refs/remotes/{ctx.remote}/{ctx.branch}"],
                          label="git-fetch", kind="gate-git", timeout_cap=gates.FETCH_TIMEOUT_S)
    if fetch.classification != "succeeded":
        missing = "couldn't find remote ref" in gates.output_of(fetch).lower()
        return "absent" if missing else "unknown"
    remote = wt.git("-C", str(ctx.worktree), "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/{ctx.remote}/{ctx.branch}", check=False)
    if not remote:
        return "absent"
    if remote == head:
        return "synchronized"
    return "behind" if wt.is_ancestor(ctx.worktree, remote, head) else "diverged"


def push_branch(ctx, operations: list[dict], head: str) -> tuple[bool, str]:
    """Fast-forward the remote run branch to `head`; never another branch, never a force push."""
    from runner import gates

    def push() -> tuple[bool, str]:
        receipt = gates.execute(ctx, [gates.binary("git"), "-C", str(ctx.worktree), "push", "--quiet", ctx.remote,
                                      f"HEAD:refs/heads/{ctx.branch}"], label="git-push", kind="gate-git",
                                timeout_cap=gates.FETCH_TIMEOUT_S)
        if receipt.classification == "succeeded" or remote_state(ctx, head) == "synchronized":
            return True, f"pushed {head[:12]}"
        return False, f"git push {gates.describe(receipt)}: {gates.first_lines(gates.output_of(receipt), 2)}"
    return attempt(ctx, operations, "push", head, None, push)


def push_head(ctx, operations: list[dict]) -> str:
    """The foreman's `publish`: push the run branch when it is absent or behind, whatever the review says.

    The pull request itself still waits for `prerequisites`, so nothing unreviewed gets published as ready.
    """
    head = wt.head(ctx.worktree)
    state = remote_state(ctx, head)
    if state in ("absent", "behind"):
        done, detail = push_branch(ctx, operations, head)
        return detail if done else f"push failed: {detail}"
    return f"remote branch is {state}; nothing to push"


def publish(ctx, data: dict) -> None:
    """Publish what is missing; failures fall through to the gate, which reports the precise state."""
    from runner import gates
    ready, head = prerequisites(ctx)
    if not ready:
        data["publication"] = {"runner": "skipped", "why": head}
        return
    operations: list[dict] = []
    data["operations"] = operations
    body = ctx.plan_dir / "pr.md"
    body_text = body.read_text(encoding="utf-8")
    body_sha = records.sha256_bytes(body_text.encode("utf-8"))

    if remote_state(ctx, head) in ("absent", "behind"):
        push_branch(ctx, operations, head)

    prs, error = gates.open_prs(ctx)
    if prs == []:
        title = next((line.strip() for line in body_text.splitlines()
                      if line.strip() and not line.startswith("#")), ctx.plan)[:70]

        def create() -> tuple[bool, str]:
            code, out, err, receipt = gates.gh_gate(ctx, ["pr", "create", "--base", ctx.base or "main", "--head",
                                                          ctx.branch or "", "--title", title, "--body-file", str(body)],
                                                    "gh-pr-create")
            faults.point("gate.after_pr_create")
            existing, _ = gates.open_prs(ctx)
            if existing:
                return True, f"PR #{existing[0].get('number')} is open" + ("" if code == 0 else " (after a failed response)")
            return False, f"gh pr create failed ({code}): {gates.first_lines(err or out, 2)}"
        attempt(ctx, operations, "pr-create", head, body_sha, create)
        prs, error = gates.open_prs(ctx)
    if prs and len(prs) == 1:
        number = prs[0]["number"]
        view, error = gates.pr_view(ctx, number)
        if view is not None and gates.evidence_text(view.get("body") or "") != gates.evidence_text(body_text):
            def edit() -> tuple[bool, str]:
                code, out, err, _ = gates.gh_gate(ctx, ["pr", "edit", str(number), "--body-file", str(body)],
                                                  "gh-pr-edit")
                current, _ = gates.pr_view(ctx, number)
                if current and gates.evidence_text(current.get("body") or "") == gates.evidence_text(body_text):
                    return True, f"PR #{number} body updated"
                return False, f"gh pr edit failed ({code}): {gates.first_lines(err or out, 2)}"
            attempt(ctx, operations, "pr-edit", head, body_sha, edit)
    data["publication"] = {"runner": "ran" if operations else "nothing to publish"}


def note_overrides(ctx, overrides: list[dict], operations: list[dict]) -> str:
    """Append an Overrides section to pr.md and the open pull request, so every accepted gate failure is public."""
    from runner import gates
    body = ctx.plan_dir / "pr.md"
    if not body.is_file():
        return "no pr.md to note the overrides in"
    text = body.read_text(encoding="utf-8")
    if re.search(r"^##\s+Overrides\s*$", text, re.M) is None:
        lines = ["", "## Overrides", "", "The factory foreman accepted these stages over a failing gate:", ""]
        for entry in overrides:
            lines.append(f"- {entry['stage']} attempt {entry['attempt']}: `{entry['gate_code']}` "
                         f"({records.bounded(entry.get('reason') or '', 200)}). Justification: "
                         f"{records.bounded(entry.get('justification') or '', 400)}")
        text = text.rstrip() + "\n" + "\n".join(lines) + "\n"
        body.write_text(text, encoding="utf-8")
    prs, error = gates.open_prs(ctx)
    if not prs:
        return f"pr.md notes the overrides; no open pull request to update ({error or 'none open'})"
    number = prs[0]["number"]
    view, _ = gates.pr_view(ctx, number)
    if view and re.search(r"^##\s+Overrides\s*$", view.get("body") or "", re.M):
        return f"PR #{number} already notes the overrides"
    head = wt.head(ctx.worktree)
    body_sha = records.sha256_bytes(text.encode("utf-8"))

    def edit() -> tuple[bool, str]:
        code, out, err, _ = gates.gh_gate(ctx, ["pr", "edit", str(number), "--body-file", str(body)], "gh-pr-overrides")
        current, _ = gates.pr_view(ctx, number)
        if current and re.search(r"^##\s+Overrides\s*$", current.get("body") or "", re.M):
            return True, f"PR #{number} notes {len(overrides)} override(s)"
        return False, f"gh pr edit failed ({code}): {gates.first_lines(err or out, 2)}"
    done, detail = attempt(ctx, operations, "pr-overrides", head, body_sha, edit)
    return detail if done else f"could not update PR #{number}: {detail}"
