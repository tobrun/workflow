#!/usr/bin/env bash
# runner.sh - one dev:remote run inside a sandbox workspace.
#
#   runner.sh run              one round of the run, as round.json describes it: the
#                              checkout to prepare, then the phases from its first one
#   runner.sh queued ROUND     mark a new round as starting before its unit starts, so no one
#                              reads the last round's ending as this round's
#   runner.sh state            status.json, unit liveness and idle time, as JSON
#   runner.sh log OFFSET TAIL  the current phase's stream-json log after line OFFSET
#                              (OFFSET 0: the last TAIL lines); first line "# offset N"
#   runner.sh pack             tar.gz on stdout of everything collect brings home
#
# remote.py pushes this file into ~/.dev-workflow/runs/<id>/ with run.json,
# round.json, contract.md, plan/ and the dev plugin under dev/, and starts `run`
# as the transient unit dev-run-<id>. Dispatch is round 1; every resume is a
# new round that refreshes these files and names where to start: a phase's own
# session continued, or a phase started fresh. On a workspace desktop, open_view puts a terminal
# that follows the run (view.py) in front of anyone watching over VNC.
# status.json is the run's state machine
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
# Where Claude Code keeps the transcripts of sessions run in $WORK, which --resume reads.
PROJECTS="$HOME/.claude/projects/$(printf %s "$WORK" | sed 's/[^A-Za-z0-9]/-/g')"

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

rnd() { # rnd KEY [INNER] - read round.json
  python3 -c 'import json, sys
try:
    d = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    d = {}
v = d.get(sys.argv[2])
if len(sys.argv) > 3:
    v = v.get(sys.argv[3]) if isinstance(v, dict) else None
print("" if v is None else v)' "$RUN_DIR/round.json" "$@"
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

# prepare CHECKOUT - fresh: the sent branch, clean; sync: the sent branch, keeping the
# working tree when it already stands on the sent commit (a session continued where it
# stopped); as-is: the checkout and plan files untouched (a round on uncollected work).
prepare() {
  local branch base default origin
  [ "$1" = as-is ] && return 0
  branch=$(cfg branch); base=$(cfg base); default=$(cfg default_branch); origin=$(cfg origin_url)
  [ -d "$WORK/.git" ] || { status failed reason="no checkout at $WORK (fleet sync did not run)"; exit 1; }
  cd "$WORK"
  if [ "$1" = sync ] && [ "$(git branch --show-current)" = "$branch" ] \
    && [ "$(git rev-parse -q --verify HEAD)" = "$(git rev-parse -q --verify "refs/remotes/origin/$branch")" ]; then
    : # the tree the session left, uncommitted work included
  else
    if [ -n "$(git status --porcelain)" ]; then
      git diff HEAD >"$RUN_DIR/pre-existing.patch" 2>/dev/null || true
    fi
    git checkout -q -f -B "$branch" "refs/remotes/origin/$branch"
    git reset -q --hard
    git clean -q -fd
  fi
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

# restore_sessions - transcripts remote.py sent with the round (sessions-in/) go where
# Claude Code looks for them, never over a transcript this workspace already has.
restore_sessions() {
  [ -d "$RUN_DIR/sessions-in" ] || return 0
  python3 - "$RUN_DIR/sessions-in" "$PROJECTS" <<'PY'
import os, shutil, sys
src, dst = sys.argv[1:]
for root, _dirs, files in os.walk(src):
    for name in files:
        rel = os.path.relpath(os.path.join(root, name), src)
        target = os.path.join(dst, rel)
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(root, name), target)
PY
}

# archive_patch PHASE - move an earlier session's uncommitted-PHASE.patch out of collect's
# way before PHASE runs again; prints where it went.
archive_patch() {
  local patch="$RUN_DIR/uncommitted-$1.patch" kept
  [ -f "$patch" ] || return 0
  mkdir -p "$RUN_DIR/history"
  kept="$RUN_DIR/history/uncommitted-$1-$(date -u +%Y%m%dT%H%M%SZ).patch"
  mv "$patch" "$kept"
  printf '%s' "$kept"
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

continue_prompt() { # continue_prompt PHASE
  printf 'Continue the %s run from where it stopped. The plan directory .dev/%s holds what was already recorded; check the working tree and the plan files before acting.' \
    "$(prompt_for "$1")" "$PLAN"
}

# run_phase PHASE [SESSION-TO-RESUME] [PROMPT] - a fresh session without a session id;
# the phase's own command, or the continue prompt, without a prompt.
run_phase() {
  local phase=$1 resume=${2:-} sid prompt=${3:-} rc result session model
  model=$(cfg model)
  cd "$WORK"
  archive_patch "$phase" >/dev/null
  if [ -n "$resume" ]; then
    sid=$resume
    prompt=${prompt:-$(continue_prompt "$phase")}
  else
    sid=$(python3 -c 'import uuid; print(uuid.uuid4())')
    prompt=${prompt:-$(prompt_for "$phase")}
  fi
  status running phase="$phase" "session.$phase=$sid" "dirty.$phase=" reason=
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
  # Every session the run ever had, so pack brings each transcript home.
  printf '%s %s\n' "$phase" "${session:-$sid}" >>"$RUN_DIR/sessions.txt"
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

# open_view - a terminal on the workspace desktop that follows this run through view.py, for an
# operator watching over VNC. gnome-terminal hands the window to the desktop session's terminal
# server, so it lives outside this unit: it outlasts the run, showing how it ended, until the
# next run on this workspace closes it. A viewer already following this run is kept (resume).
# Best effort: a desktop that cannot show it never fails the run.
open_view() {
  [ "$(cfg view)" != False ] || return 0
  [ -n "${DISPLAY:-}" ] && command -v gnome-terminal >/dev/null 2>&1 || return 0
  local pid args mine=false
  for pid in $(pgrep -u "$(id -u)" -f 'skills/remote/scripts/view\.py' 2>/dev/null || true); do
    args=$(ps -o args= -p "$pid" 2>/dev/null) || continue
    case "$args " in
      *" $RUN_DIR "*) mine=true ;;
      *" $(dirname "$RUN_DIR")/"*) kill "$pid" 2>/dev/null || true ;;  # an earlier run on this workspace
    esac
  done
  $mine && return 0
  if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
  fi
  gnome-terminal --maximize --title "dev:remote $RUN_ID" -- \
    python3 "$RUN_DIR/dev/skills/remote/scripts/view.py" "$RUN_DIR" >>"$LOGS/view.err" 2>&1 || true
}

setup_env() {
  mkdir -p "$LOGS"
  if [ -r /etc/fleet/computer-use.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/fleet/computer-use.env
    set +a
  fi
  # How long a headless phase waits for its background agents before Claude Code ends it.
  # The 600 s default killed a ship fix agent mid-run, so allow ten times that.
  export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-6000000}"
  github_env
  trap 'p=$(field phase); [ -z "$p" ] || save_dirty "$p" || true; status stopped reason="stopped by signal"; exit 143' TERM INT
  # Any other early exit (set -e) still ends in a recorded state, never a silent "lost".
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then case $(field state) in starting|running)
    status failed reason="runner exited $rc";; esac; fi' EXIT
}

# start_prompt PHASE MODE EXTRA - the prompt of a round's first session: the phase's own
# command (fresh) or the continue prompt, naming uncommitted work the checkout no longer
# holds, then the operator's own instruction.
start_prompt() {
  local phase=$1 mode=$2 extra=$3 text kept
  if [ "$mode" = continue ]; then text=$(continue_prompt "$phase"); else text=$(prompt_for "$phase"); fi
  if [ "$mode" = continue ] && [ -z "$(git -C "$WORK" status --porcelain 2>/dev/null)" ]; then
    kept=$(archive_patch "$phase")
    if [ -n "$kept" ]; then
      text="$text Uncommitted work the earlier session left is not in this checkout; its diff is at $kept."
    fi
  fi
  if [ -n "$extra" ]; then text="$text

$extra"; fi
  printf '%s' "$text"
}

cmd_run() {
  setup_env
  local round first mode sid extra phase started=true
  round=$(rnd round); first=$(rnd first phase); mode=$(rnd first mode)
  sid=$(rnd first session); extra=$(rnd first prompt)
  if [ "${round:-1}" -le 1 ]; then
    status starting run="$RUN_ID" home="$HOME" started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  else
    status running round="$round" phase= reason=
  fi
  open_view
  restore_sessions
  prepare "$(rnd checkout)"
  [ -z "$first" ] || started=false
  for phase in $(cfg phases); do
    if ! $started; then
      [ "$phase" = "$first" ] || continue
      started=true
      if [ "$mode" = continue ]; then
        run_phase "$phase" "$sid" "$(start_prompt "$phase" continue "$extra")" || exit 1
      else
        run_phase "$phase" "" "$(start_prompt "$phase" fresh "$extra")" || exit 1
      fi
    else
      run_phase "$phase" || exit 1
    fi
    gate "$phase" || exit 1
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
  python3 - "$RUN_DIR" "$WORK" "$PLAN" "$TMP_ROOT/$REPO" "$PROJECTS" <<'PY'
import glob, json, os, sys, tarfile
run_dir, work, plan, tmp, projects = sys.argv[1:]
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as out:
    def add(src, name):
        if os.path.exists(src):
            out.add(src, arcname=name)
    add(os.path.join(work, ".dev", plan), "plan")
    add(os.path.join(tmp, "reports"), "reports")
    add(os.path.join(tmp, "pr-evidence"), "pr-evidence")
    for name in ("logs", "status.json", "round.json", "sessions.txt"):
        add(os.path.join(run_dir, name), "run/" + name)
    for extra in glob.glob(os.path.join(run_dir, "*.patch")) + glob.glob(os.path.join(run_dir, "gate-*.txt")):
        add(extra, "run/" + os.path.basename(extra))
    # Every session's transcript, with its subagents and tool results, so a later round can
    # continue any of them on any workspace.
    sessions = set()
    try:
        status = json.load(open(os.path.join(run_dir, "status.json")))
        sessions.update(v for v in (status.get("session") or {}).values() if v)
    except (OSError, ValueError, AttributeError):
        pass
    try:
        sessions.update(line.split()[1] for line in open(os.path.join(run_dir, "sessions.txt")) if len(line.split()) > 1)
    except OSError:
        pass
    for sid in sorted(sessions):
        add(os.path.join(projects, sid + ".jsonl"), f"sessions/{sid}.jsonl")
        add(os.path.join(projects, sid), f"sessions/{sid}")
PY
}

case "${1:-}" in
  run) cmd_run ;;
  queued)
    if [ "${2:-1}" -gt 1 ]; then
      status starting round="$2" resumed="$(date -u +%Y-%m-%dT%H:%M:%SZ)" phase= reason=
    else
      status starting round=1
    fi ;;
  state) cmd_state ;;
  log) shift; cmd_log "$@" ;;
  pack) cmd_pack ;;
  *) echo "usage: runner.sh run|queued ROUND|state|log OFFSET TAIL|pack" >&2; exit 2 ;;
esac
