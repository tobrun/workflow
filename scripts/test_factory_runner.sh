#!/usr/bin/env bash
# Offline end-to-end test of the factory CLI with stub codex, claude, and gh binaries.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
FACTORY="$ROOT/factory/bin/factory"
STUBS="$ROOT/factory/runner/tests/stubs"
TEMP_DIR=$(mktemp -d)
trap 'for d in "$TEMP_DIR"/home/runs/*/; do [ -d "$d" ] && touch "$d/cancel"; done; sleep 1; rm -rf "$TEMP_DIR"' EXIT

export FACTORY_HOME="$TEMP_DIR/home"
export FACTORY_CODEX_BIN="$STUBS/codex"
export FACTORY_CLAUDE_BIN="$STUBS/claude"
export FACTORY_GH_BIN="$STUBS/gh"
export FACTORY_STUB_LOG="$TEMP_DIR/stub-log.jsonl"
export FACTORY_STUB_SCENARIO="$TEMP_DIR/scenario.json"
export FACTORY_CLAUDE_SCENARIO="$TEMP_DIR/claude.json"
export FACTORY_GH_STATE="$TEMP_DIR/gh-state.json"
export GIT_CONFIG_GLOBAL="$TEMP_DIR/gitconfig"
export CODEX_HOME="$TEMP_DIR/codex-home"
export GIT_AUTHOR_NAME="Factory E2E" GIT_AUTHOR_EMAIL="factory-e2e@example.com"
export GIT_COMMITTER_NAME="Factory E2E" GIT_COMMITTER_EMAIL="factory-e2e@example.com"
export PYTHONPATH="$ROOT/factory"

mkdir -p "$FACTORY_HOME"
printf '[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n' > "$GIT_CONFIG_GLOBAL"
# The stub hosts play stages, not the foreman: this test drives the fixed pipeline under model.decide().
printf '{"notify": false, "stage_poll_seconds": 0.05, "heartbeat_seconds": 0.2, "browser": "off", "foreman": "off"}\n' > "$FACTORY_HOME/config.json"
printf '{"auth": true, "prs": [], "checks": {}, "default_checks": [{"name": "ci", "state": "SUCCESS", "bucket": "pass"}]}\n' \
  > "$FACTORY_GH_STATE"

fail() {
  echo "factory runner e2e: $*" >&2
  exit 1
}

make_repo() {
  local name="$1"
  git init --quiet --bare -b main "$TEMP_DIR/$name-origin.git"
  git init --quiet -b main "$TEMP_DIR/$name"
  git -C "$TEMP_DIR/$name" config user.name "Factory E2E"
  git -C "$TEMP_DIR/$name" config user.email "factory-e2e@example.com"
  # The same tiny application the unit tests use: a buggy base, a test runner, and a contract.
  python3 -c 'import sys; from pathlib import Path; from runner.tests.helpers import seed_repo; seed_repo(Path(sys.argv[1]))' \
    "$TEMP_DIR/$name"
  git -C "$TEMP_DIR/$name" add -A
  git -C "$TEMP_DIR/$name" commit --quiet -m initial
  git -C "$TEMP_DIR/$name" remote add origin "$TEMP_DIR/$name-origin.git"
  git -C "$TEMP_DIR/$name" push --quiet -u origin main
  git -C "$TEMP_DIR/$name" symbolic-ref refs/remotes/origin/HEAD refs/remotes/origin/main
}

# Scenarios come from the same fixture builders the unit tests use.
python3 - "$TEMP_DIR" <<'PY'
import json, sys
from pathlib import Path
from runner.tests.helpers import happy_scenario, scope_files, ship_step
root = Path(sys.argv[1])
(root / "claude.json").write_text(json.dumps([scope_files()]))
(root / "scenario-happy.json").write_text(json.dumps(happy_scenario()))
blocked = happy_scenario()
blocked["ship"] = [ship_step("BLOCK", draft=True, blockers="[security] webhook.py:3 - replay window unbounded\n")]
(root / "scenario-blocked.json").write_text(json.dumps(blocked))
PY

wait_terminal() {
  local run_dir="$1" deadline=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if python3 - "$run_dir" <<'PY'
import json, sys
from pathlib import Path
from runner import supervise
run_dir = Path(sys.argv[1])
status = json.loads((run_dir / "run.json").read_text())["status"]
sys.exit(0 if status in ("done", "needs-human", "cancelled") and not supervise.worker_alive(run_dir) else 1)
PY
    then
      return 0
    fi
    sleep 0.2
  done
  cat "$run_dir/worker.log" >&2 || true
  fail "run in $run_dir did not reach a terminal status"
}

# --- Scenario 1: scope handoff to a ready pull request -------------------------
make_repo happy
cp "$TEMP_DIR/scenario-happy.json" "$FACTORY_STUB_SCENARIO"
# Attached (the default): `new` stays in the terminal until the run finishes.
"$FACTORY" new "$TEMP_DIR/happy" "Add idempotency protection to webhook processing" \
  --plan webhook-idempotency --yes > "$TEMP_DIR/new-happy.out" \
  || fail "attached run did not exit 0: $(cat "$TEMP_DIR/new-happy.out")"
grep -q "Watching the assembly line" "$TEMP_DIR/new-happy.out" || fail "did not attach: $(cat "$TEMP_DIR/new-happy.out")"
grep -q "build attempt 1 started" "$TEMP_DIR/new-happy.out" || fail "no live stage updates"
tail -1 "$TEMP_DIR/new-happy.out" | grep -q "^Done: https://github.com/stub/repo/pull/1$" \
  || fail "attached view did not end with the PR: $(tail -3 "$TEMP_DIR/new-happy.out")"
HAPPY_DIR=$(ls -d "$FACTORY_HOME"/runs/*-webhook-idempotency)
wait_terminal "$HAPPY_DIR"

python3 - "$HAPPY_DIR" "$FACTORY_GH_STATE" <<'PY'
import json, sys
from pathlib import Path
run_dir, gh_state = Path(sys.argv[1]), Path(sys.argv[2])
run = json.loads((run_dir / "run.json").read_text())
events = [json.loads(line)["event"] for line in (run_dir / "events.jsonl").read_text().splitlines()]

def check(condition, message):
    if not condition:
        sys.exit(f"scenario 1: {message}")

check(run["status"] == "done", f"status {run['status']}: {run['human']}")
check(run["retries"] == {"used": 0, "budget": 5}, f"retries {run['retries']}")
check([a["stage"] for a in run["attempts"]] == ["scope", "scope-review", "build", "ship"], "attempt order")
check(all(a["outcome"] == "done" for a in run["attempts"]), "every attempt passed")
milestones = ["run.created", "worktree.created", "run.scoping", "scope.launched", "scope.committed", "run.queued",
              "slot.acquired", "stage.started", "process.spawned", "process.exited", "slot.released",
              "gate.evaluated", "stage.finished", "run.done"]
positions = [events.index(name) for name in milestones]
check(positions == sorted(positions), f"event ordering {events}")
check(events.count("stage.started") == 3 and events.count("slot.released") == 3, "one slot per headless stage")
artifacts = run["artifacts"]
check(artifacts["spec_review"] == ".dev/webhook-idempotency/spec-review_1.md", f"spec review {artifacts}")
check(artifacts["notes"] == ".dev/webhook-idempotency/implementation-notes.md", "notes path")
check(artifacts["e2e_report"] == "reports/webhook-idempotency-e2e.json", f"e2e path {artifacts['e2e_report']}")
check(artifacts["review"] == ".dev/webhook-idempotency/review_1.md", "review path")
check((run_dir / artifacts["e2e_report"]).is_file(), "e2e report exists under the run directory")
pr = run["pr"]
check(pr["number"] == 1 and pr["url"] == "https://github.com/stub/repo/pull/1" and pr["draft"] is False, f"pr {pr}")
state = json.loads(gh_state.read_text())
check(len(state["prs"]) == 1 and state["prs"][0]["headRefName"] == "factory/webhook-idempotency", "one PR on run branch")
check((run_dir / "dashboard.html").exists() is False, "dashboard lives at the runtime root")
check((run_dir.parent.parent / "dashboard.html").is_file(), "dashboard regenerated")
PY

"$FACTORY" show "$(basename "$HAPPY_DIR")" > "$TEMP_DIR/show-happy.out"
head -1 "$TEMP_DIR/show-happy.out" | grep -q "^Done: pull request https://github.com/stub/repo/pull/1 is ready.$" \
  || fail "show does not lead with the outcome: $(head -1 "$TEMP_DIR/show-happy.out")"
git -C "$HAPPY_DIR/worktree" log --format='%an %s%n%b' | grep -q "Co-Authored-By" \
  && fail "a co-author trailer was added"

# --- Scenario 2: the same retryable block exhausts the shared budget ------------
make_repo blocked
cp "$TEMP_DIR/scenario-blocked.json" "$FACTORY_STUB_SCENARIO"
printf '{"auth": true, "prs": [], "checks": {}, "default_checks": []}\n' > "$FACTORY_GH_STATE"
# Detached: `new --detach` returns after the handoff; `watch` reattaches and exits 1 when the run cancels.
"$FACTORY" new "$TEMP_DIR/blocked" "Bound the webhook replay window" --plan replay-window --yes --detach \
  > "$TEMP_DIR/new-blocked.out"
grep -q "Handed off: worker" "$TEMP_DIR/new-blocked.out" || fail "detached handoff: $(cat "$TEMP_DIR/new-blocked.out")"
BLOCKED_DIR=$(ls -d "$FACTORY_HOME"/runs/*-replay-window)
if "$FACTORY" watch "$(basename "$BLOCKED_DIR")" > "$TEMP_DIR/watch-blocked.out"; then
  fail "watch exited 0 for a cancelled run"
fi
grep -q "^Cancelled at ship: retry budget exhausted (5/5)" "$TEMP_DIR/watch-blocked.out" \
  || fail "watch did not report the cancellation: $(tail -3 "$TEMP_DIR/watch-blocked.out")"
wait_terminal "$BLOCKED_DIR"

python3 - "$BLOCKED_DIR" "$FACTORY_HOME" <<'PY'
import json, sys
from pathlib import Path
from runner import slots
run_dir, home = Path(sys.argv[1]), Path(sys.argv[2])
run = json.loads((run_dir / "run.json").read_text())

def check(condition, message):
    if not condition:
        sys.exit(f"scenario 2: {message}")

ships = [a for a in run["attempts"] if a["stage"] == "ship"]
check(run["status"] == "cancelled", f"status {run['status']}")
check(len(ships) == 6, f"{len(ships)} ship attempts")
check(all(a["outcome"] == "blocked" for a in ships), "every ship attempt blocked")
check(run["retries"] == {"used": 5, "budget": 5}, f"retries {run['retries']}")
reason = run["human"]["reason"]
check("retry budget exhausted (5/5)" in reason and "replay window unbounded" in reason, f"reason {reason}")
check(slots.holders(home / "slots") == {}, "slot released")
check((run_dir / "worktree").is_dir(), "worktree retained")
check(run["pr"]["number"] == 1 and run["pr"]["draft"] is True, f"draft PR retained {run['pr']}")
PY

"$FACTORY" ls > "$TEMP_DIR/ls.out"
head -1 "$TEMP_DIR/ls.out" | grep -q "^1 need you, 0 in flight.$" || fail "ls headline: $(head -1 "$TEMP_DIR/ls.out")"
grep -q "factory retry $(basename "$BLOCKED_DIR") --reset-budget" "$TEMP_DIR/ls.out" \
  || fail "ls does not name the next command"

echo "Factory runner e2e passed."
