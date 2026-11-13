#!/usr/bin/env bash
# runner.sh - one dev:remote run inside a sandbox workspace.
#
#   runner.sh run              prepare the checkout, then run each phase
#   runner.sh resume           continue the phase that failed or stopped, then the rest
#   runner.sh state            status.json, unit liveness and idle time, as JSON
#   runner.sh log OFFSET TAIL  the current phase's stream-json log after line OFFSET
#                              (OFFSET 0: the last TAIL lines); first line "# offset N"
#   runner.sh pack             tar.gz on stdout of everything collect brings home
#
# remote.py pushes this file into ~/.dev-workflow/runs/<id>/ with run.json,
# contract.md, plan/ and the dev plugin under dev/, and starts `run` as the
# transient unit dev-run-<id>. status.json is the run's state machine
# (starting -> running -> done | failed | stopped); references/protocol.md
# owns its fields. Only commits leave the workspace, so a phase that ends
# with a dirty tree leaves its diff beside the logs as uncommitted-<phase>.patch.
set -Eeuo pipefail

RUN_DIR=$(cd "$(dirname "$0")" && pwd)
STATUS="$RUN_DIR/status.json"
LOGS="$RUN_DIR/logs"

cfg() {
  python3 -c 'import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print(" ".join(v) if isinstance(v, list) else ("" if v is None else v))' "$RUN_DIR/run.json" "$1"
}

RUN_ID=$(cfg id)
REPO=$(cfg repo)
PLAN=$(cfg plan)
WORK="$HOME/work/$REPO"
TMP_ROOT="${DEV_REMOTE_TMP:-/tmp}"

# status STATE [key=value ...] - merge into status.json atomically; a dotted
# key nests one level (session.build=...).
status() {
  python3 - "$STATUS" "$@" <<'PY'
import json, os, sys, time
path, state, *pairs = sys.argv[1:]
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {}
data["state"] = state
data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
for pair in pairs:
    key, _, value = pair.partition("=")
    if "." in key:
        outer, inner = key.split(".", 1)
        data.setdefault(outer, {})[inner] = value
    else:
        data[key] = value
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle, indent=2)
os.replace(tmp, path)
PY
}

field() { # field KEY [INNER] - read status.json
  python3 -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    d = {}
v = d.get(sys.argv[2], "")
if len(sys.argv) > 3:
    v = v.get(sys.argv[3], "") if isinstance(v, dict) else ""
print(v)' "$STATUS" "$@"
}

prompt_for() {
  case $1 in
    build) printf '/dev:build %s' "$PLAN" ;;
    ship) printf '/dev:ship plan .dev/%s' "$PLAN" ;;
    ship-quick) printf '/dev:ship quick plan .dev/%s' "$PLAN" ;;
    *) return 1 ;;
  esac
}

with_timeout_secs() { # with_timeout_secs SECS CMD... - bounded where timeout(1) exists
  local secs=$1; shift
  if command -v timeout >/dev/null 2>&1; then timeout "$secs" "$@"; else "$@"; fi
}

with_timeout() {
  local secs
  secs=$(cfg phase_timeout_seconds)
  if [ -n "$secs" ] && command -v timeout >/dev/null 2>&1; then
    timeout --kill-after=120 "$secs" "$@"
  else
    "$@"
  fi
}

# Ending state of a phase from its stream: "ok", or the reason it is not.
outcome() {
  python3 - "$1" <<'PY'
import json, sys
result, session = None, ""
try:
    lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    lines = []
for line in lines:
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if event.get("type") == "system" and event.get("subtype") == "init":
        session = event.get("session_id") or session
    if event.get("type") == "result":
        result = event
if result is None:
    print("no-result", session)
elif result.get("subtype") != "success":
    print("error-" + str(result.get("subtype") or "unknown"), session)
elif result.get("is_error"):  # a "success" subtype that carries an API error, such as a gateway 500
    print("api-error", session)
else:
    print("ok", session)
PY
}

prepare() {
  local branch base default origin
  branch=$(cfg branch); base=$(cfg base); default=$(cfg default_branch); origin=$(cfg origin_url)
  [ -d "$WORK/.git" ] || { status failed reason="no checkout at $WORK (fleet sync did not run)"; exit 1; }
  cd "$WORK"
  if [ -n "$(git status --porcelain)" ]; then
    git diff HEAD >"$RUN_DIR/pre-existing.patch" 2>/dev/null || true
  fi
  git checkout -q -f -B "$branch" "refs/remotes/origin/$branch"
  git reset -q --hard
  git clean -q -fd
  # The merge base the operator dispatched from: ship's diff scope reads origin/<default>.
  git update-ref "refs/remotes/origin/$default" "$base"
  git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$default"
  if [ -n "$origin" ]; then git remote set-url origin "$origin"; fi
  rm -rf ".dev/$PLAN"
  mkdir -p ".dev/$PLAN"
  cp -R "$RUN_DIR/plan/." ".dev/$PLAN/"
  if [ -d "$RUN_DIR/tmp" ]; then
    mkdir -p "$TMP_ROOT/$REPO"
    cp -R "$RUN_DIR/tmp/." "$TMP_ROOT/$REPO/"
  fi
}

# save_dirty PHASE - keep a dirty tree's diff beside the logs; true when there was one.
save_dirty() {
  [ -d "$WORK/.git" ] || return 1
  [ -n "$(git -C "$WORK" status --porcelain)" ] || return 1
  { git -C "$WORK" diff HEAD; git -C "$WORK" ls-files --others --exclude-standard | sed 's/^/untracked: /'; } \
    >"$RUN_DIR/uncommitted-$1.patch" 2>/dev/null || true
}

# record_pr - after a ship phase that may publish, read the branch's pull request
# back from the host into status.json (pr.url, pr.number, pr.state, pr.draft).
record_pr() {
  [ "$(cfg publish)" = sandbox ] && command -v gh >/dev/null 2>&1 || return 0
  local json pairs
  json=$(cd "$WORK" && with_timeout_secs 60 gh pr view "$(cfg branch)" --json url,number,state,isDraft 2>/dev/null) || return 0
  pairs=$(printf '%s' "$json" | python3 -c 'import json, sys
pr = json.load(sys.stdin)
pr["isDraft"] = "true" if pr.get("isDraft") else "false"
print(" ".join("pr.%s=%s" % (key, pr.get(field, "")) for key, field in
               (("url", "url"), ("number", "number"), ("state", "state"), ("draft", "isDraft"))))') || return 0
  # shellcheck disable=SC2086
  status running $pairs
}

# run_phase PHASE [SESSION-TO-RESUME [PROMPT]]
run_phase() {
  local phase=$1 resume=${2:-} sid prompt=${3:-} rc result session model
  model=$(cfg model)
  cd "$WORK"
  if [ -n "$resume" ] && [ -n "$prompt" ]; then
    sid=$resume
  elif [ -n "$resume" ]; then
    sid=$resume
    prompt="Continue the $(prompt_for "$phase") run from where it stopped. The plan directory .dev/$PLAN holds what was already recorded; check the working tree and the plan files before acting."
  else
    sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    prompt=$(prompt_for "$phase")
  fi
  status running phase="$phase" "session.$phase=$sid" reason=
  # No agent attribution in commits or PR text: the workflow's commit rules forbid Co-Authored-By lines.
  printf '%s\n' '{"includeCoAuthoredBy": false, "attribution": {"commit": "", "pr": ""}}' >"$RUN_DIR/settings.json"
  local -a args=(-p "$prompt" --plugin-dir "$RUN_DIR/dev" --append-system-prompt-file "$RUN_DIR/contract.md"
    --settings "$RUN_DIR/settings.json" --permission-mode bypassPermissions --disallowed-tools AskUserQuestion
    --output-format stream-json --verbose)
  if [ -n "$model" ]; then args+=(--model "$model"); fi
  if [ -n "$resume" ]; then args+=(--resume "$resume"); else args+=(--session-id "$sid"); fi
  set +e
  with_timeout claude "${args[@]}" >>"$LOGS/$phase.jsonl" 2>>"$LOGS/$phase.err" </dev/null
  rc=$?
  set -e
  read -r result session <<<"$(outcome "$LOGS/$phase.jsonl")"
  if [ -n "$session" ]; then status running "session.$phase=$session"; fi
  if save_dirty "$phase"; then status running "dirty.$phase=true"; fi
  case $phase in ship|ship-quick) record_pr ;; esac
  status running "head.$phase=$(git rev-parse HEAD)" "exit.$phase=$rc"
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then
    status failed reason="$phase timed out"; return 1
  fi
  if [ "$rc" -ne 0 ] || [ "$result" != ok ]; then
    status failed reason="$phase ended with exit $rc ($result)"; return 1
  fi
  status running "completed.$phase=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}

GATE_RESUMES=2

# gate PHASE - a phase's own completion check, run by the runner because nobody
# watches the session: build is done only when check-tests.py maps every spec
# scenario to a passing, receipted runner case. A failing gate resumes the
# phase's session with the checker's output, up to GATE_RESUMES times.
gate() {
  [ "$1" = build ] || return 0
  local tries=0 out
  while :; do
    if out=$(cd "$WORK" && python3 "$RUN_DIR/dev/skills/build/scripts/check-tests.py" ".dev/$PLAN" 2>&1); then
      status running "gate.build=pass"
      return 0
    fi
    printf '%s\n' "$out" >"$RUN_DIR/gate-build.txt"
    if [ "$tries" -ge "$GATE_RESUMES" ]; then
      status failed "gate.build=fail" reason="build gate: check-tests.py still fails after $tries resumes (gate-build.txt)"
      return 1
    fi
    tries=$((tries + 1))
    status running "gate.build=resume $tries"
    run_phase build "$(field session build)" "The /dev:build run is not finished: its completion gate, \`python3 $RUN_DIR/dev/skills/build/scripts/check-tests.py .dev/$PLAN\`, fails with:

$out

Finish the build skill as written: keep test-evidence.json with runner receipts captured through check-tests.py capture (steps 5 and 6 and references/test-evidence.md), loop the gate until it passes, commit, and end the session." || return 1
  done
}

# github_env - gh infers the repository from the remotes, and a host that rewrites github.com
# URLs to a credential relay (insteadOf) hides it. GH_REPO names it for every gh command;
# `gh pr create` still resolves the branch's push remote unless --head is given, so a wrapper
# first on PATH adds --head for the current branch when the call names none.
github_env() {
  local gh_repo real
  gh_repo=$(cfg github_repo)
  real=$(command -v gh 2>/dev/null) || return 0
  [ -n "$gh_repo" ] || return 0
  export GH_REPO="$gh_repo"
  mkdir -p "$RUN_DIR/bin"
  cat >"$RUN_DIR/bin/gh" <<WRAPPER
#!/usr/bin/env bash
# dev:remote wrapper: see github_env in runner.sh.
if [ "\${1:-}" = pr ] && [ "\${2:-}" = create ]; then
  for arg in "\$@"; do
    case \$arg in --head|--head=*|-H|-H?*) exec "$real" "\$@" ;; esac
  done
  branch=\$(git branch --show-current 2>/dev/null)
  if [ -n "\$branch" ]; then shift 2; exec "$real" pr create --head "\$branch" "\$@"; fi
fi
exec "$real" "\$@"
WRAPPER
  chmod +x "$RUN_DIR/bin/gh"
  export PATH="$RUN_DIR/bin:$PATH"
}

setup_env() {
  mkdir -p "$LOGS"
  if [ -r /etc/fleet/computer-use.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/fleet/computer-use.env
    set +a
  fi
  export DEV_MEMORY_DIR="$RUN_DIR/memory"
  github_env
  trap 'p=$(field phase); [ -z "$p" ] || save_dirty "$p" || true; status stopped reason="stopped by signal"; exit 143' TERM INT
  # Any other early exit (set -e) still ends in a recorded state, never a silent "lost".
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then case $(field state) in starting|running)
    status failed reason="runner exited $rc";; esac; fi' EXIT
}

cmd_run() {
  setup_env
  status starting run="$RUN_ID" home="$HOME" started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  prepare
  local phase
  for phase in $(cfg phases); do
    run_phase "$phase" || exit 1
    gate "$phase" || exit 1
  done
  status done phase= reason=
}

cmd_resume() {
  setup_env
  local failed sid phase started=false
  failed=$(field phase)
  [ -n "$failed" ] || { echo "runner: no phase to resume" >&2; exit 1; }
  sid=$(field session "$failed")
  status running resumed="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for phase in $(cfg phases); do
    if [ "$phase" = "$failed" ]; then
      started=true
      run_phase "$phase" "$sid" || exit 1
      gate "$phase" || exit 1
    elif $started; then
      run_phase "$phase" || exit 1
      gate "$phase" || exit 1
    fi
  done
  status done phase= reason=
}

cmd_state() {
  local active=false
  if systemctl is-active --quiet "dev-run-$RUN_ID" 2>/dev/null; then active=true; fi
  python3 - "$STATUS" "$LOGS" "$active" <<'PY'
import glob, json, os, sys, time
path, logs, active = sys.argv[1:]
try:
    status = json.load(open(path))
except (OSError, ValueError):
    status = None
stamps = [os.path.getmtime(p) for p in glob.glob(os.path.join(logs, "*.jsonl"))]
age = round(time.time() - max(stamps)) if stamps else None
print(json.dumps({"status": status, "active": active == "true", "last_event_age": age}))
PY
}

cmd_log() {
  local phase
  phase=$(field phase)
  [ -n "$phase" ] || phase=$(cfg phases | awk '{print $NF}')
  python3 - "$LOGS/$phase.jsonl" "${1:-0}" "${2:-40}" <<'PY'
import sys
path, offset, tail = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
try:
    lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
except OSError:
    lines = []
print(f"# offset {len(lines)}")
# Thinking-token ticks are nearly every line of a stream and carry no content.
kept = [l for l in (lines[-tail * 50:] if offset == 0 else lines[offset:]) if '"thinking_tokens"' not in l[:120]]
for line in (kept[-tail:] if offset == 0 else kept):
    print(line)
PY
}

cmd_pack() {
  python3 - "$RUN_DIR" "$WORK" "$PLAN" "$TMP_ROOT/$REPO" "$HOME" <<'PY'
import glob, json, os, sys, tarfile
run_dir, work, plan, tmp, home = sys.argv[1:]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as out:
    def add(src, name):
        if os.path.exists(src):
            out.add(src, arcname=name)
    add(os.path.join(work, ".dev", plan), "plan")
    add(os.path.join(work, ".dev", "metrics.jsonl"), "metrics.jsonl")
    add(os.path.join(tmp, "reports"), "reports")
    add(os.path.join(tmp, "pr-evidence"), "pr-evidence")
    add(os.path.join(run_dir, "memory"), "memory")
    add(os.path.join(run_dir, "logs"), "run/logs")
    add(os.path.join(run_dir, "status.json"), "run/status.json")
    for extra in glob.glob(os.path.join(run_dir, "*.patch")) + glob.glob(os.path.join(run_dir, "gate-*.txt")):
        add(extra, "run/" + os.path.basename(extra))
    # The transcripts the journal cites, mirrored under home/ so collect can repoint them.
    cited = set()
    for journal in glob.glob(os.path.join(run_dir, "memory", "journal", "*.jsonl")):
        for line in open(journal, encoding="utf-8"):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            cited.update(p for p in [entry.get("transcript"), *(entry.get("subagent_transcripts") or [])] if p)
    for path in sorted(cited):
        real = os.path.realpath(path)
        if real.startswith(os.path.realpath(home) + os.sep) and os.path.isfile(real):
            add(real, "home/" + os.path.relpath(real, os.path.realpath(home)))
PY
}

case "${1:-}" in
  run) cmd_run ;;
  resume) cmd_resume ;;
  state) cmd_state ;;
  log) shift; cmd_log "$@" ;;
  pack) cmd_pack ;;
  *) echo "usage: runner.sh run|resume|state|log OFFSET TAIL|pack" >&2; exit 2 ;;
esac
