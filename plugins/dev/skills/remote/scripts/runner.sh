#!/usr/bin/env bash
# runner.sh - the dev:remote run of one fleet instance, inside that instance.
#
#   runner.sh run              a new run: prepare the checkout, then work until it is finished
#   runner.sh continue         the same run again, in place, in its own session
#   runner.sh state            status.json, unit liveness and idle time, as JSON
#   runner.sh log OFFSET TAIL  the run's stream-json log after line OFFSET
#                              (OFFSET 0: the last TAIL lines); first line "# offset N"
#   runner.sh pack             tar.gz on stdout of what `pull` brings home
#   runner.sh bundle           a git bundle on stdout of the work branch since the base
#
# remote.py puts this file in ~/.dev-workflow/run/ with run.json, prompt.md,
# contract.md, the plan directory, a bundle of the sent commits and the dev
# plugin under dev/, and starts it as the transient unit dev-remote. An
# instance holds one run; the one before it moves to ~/.dev-workflow/history/.
#
# The run is one Claude Code session that the runner keeps going: whenever the
# session ends, `unfinished` checks the deliverable (everything committed, the
# branch pushed, its pull request open), and while that fails the same session
# is resumed and told what is missing. It ends as done, as blocked (the session
# wrote blocked.md: a hard stop), or as failed once the attempts run out.
set -Eeuo pipefail

RUN_DIR=$(cd "$(dirname "$0")" && pwd)
STATUS="$RUN_DIR/status.json"
LOG="$RUN_DIR/log.jsonl"
UNIT=dev-remote

cfg() {
  python3 -c 'import json, sys
v = json.load(open(sys.argv[1])).get(sys.argv[2])
print("" if v is None else v)' "$RUN_DIR/run.json" "$1"
}

REPO=$(cfg repo)
WORK="$HOME/work/$REPO"
# Where Claude Code keeps the transcripts of sessions run in $WORK, which --resume reads.
PROJECTS="$HOME/.claude/projects/$(printf %s "$WORK" | sed 's/[^A-Za-z0-9]/-/g')"

# status STATE [key=value ...] - merge into status.json atomically; a dotted key nests one level.
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
        if not isinstance(data.get(outer), dict):
            data[outer] = {}
        data[outer][inner] = value
    else:
        data[key] = value
tmp = path + ".tmp"
with open(tmp, "w") as handle:
    json.dump(data, handle, indent=2)
os.replace(tmp, path)
PY
}

field() { # field KEY - read status.json
  python3 -c 'import json, sys
try:
    v = json.load(open(sys.argv[1])).get(sys.argv[2], "")
except (OSError, ValueError):
    v = ""
print(v if isinstance(v, (str, int)) else "")' "$STATUS" "$1"
}

bounded() { # bounded SECS CMD... - where timeout(1) exists
  local secs=$1; shift
  if command -v timeout >/dev/null 2>&1; then timeout --kill-after=120 "$secs" "$@"; else "$@"; fi
}

# outcome FROM - how the session whose stream starts at line FROM of the log ended:
# "ok", or the reason it is not, then its session id.
outcome() {
  python3 - "$LOG" "$1" <<'PY'
import json, sys
result, session = None, ""
try:
    lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()[int(sys.argv[2]):]
except OSError:
    lines = []
for line in lines:
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if not isinstance(event, dict):
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

# prepare - the sent commits on the work branch, the plan files, and origin as the laptop sees it.
# Nothing the instance holds is dropped: commits an earlier run left ahead of the sent one are
# kept as the starting point, a diverged branch is saved under refs/dev-remote/kept/, and
# uncommitted leftovers go to leftover.patch.
prepare() {
  local branch sent base default origin tip start
  branch=$(cfg branch); sent=$(cfg sent); base=$(cfg base); default=$(cfg default_branch); origin=$(cfg origin_url)
  mkdir -p "$WORK"
  cd "$WORK"
  [ -d .git ] || git init -q -b "$default"
  if [ -f "$RUN_DIR/in.bundle" ]; then
    git fetch -q "$RUN_DIR/in.bundle" "+HEAD:refs/dev-remote/sent"
    rm -f "$RUN_DIR/in.bundle"
  fi
  git cat-file -e "$sent^{commit}" 2>/dev/null \
    || { status failed reason="the sent commit $sent did not arrive in $WORK"; exit 1; }
  start=$sent
  if tip=$(git rev-parse -q --verify "refs/heads/$branch"); then
    if git merge-base --is-ancestor "$sent" "$tip"; then
      start=$tip  # an earlier run here is ahead of what was sent: its commits are the work so far
    elif ! git merge-base --is-ancestor "$tip" "$sent"; then
      git update-ref "refs/dev-remote/kept/$branch-$(date -u +%Y%m%dT%H%M%SZ)" "$tip"
    fi
  fi
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    git diff HEAD >"$RUN_DIR/leftover.patch" 2>/dev/null || true
  fi
  git checkout -q -f -B "$branch" "$start"
  git clean -q -fd
  if [ -n "$origin" ]; then
    git remote get-url origin >/dev/null 2>&1 && git remote set-url origin "$origin" || git remote add origin "$origin"
  fi
  # The merge base the laptop sent from: ship's diff scope reads origin/<default>.
  git update-ref "refs/remotes/origin/$default" "$base"
  git symbolic-ref refs/remotes/origin/HEAD "refs/remotes/origin/$default"
  if [ -d "$RUN_DIR/plan" ]; then  # the laptop's plan files win; files only this instance has stay
    mkdir -p .dev
    cp -R "$RUN_DIR/plan/." .dev/
  fi
  status running branch="$branch" start="$start"
}

# unfinished - nothing when the deliverable is there, else what is missing. The pull request it
# finds is recorded in status.json.
unfinished() {
  local branch default dirty json head
  default=$(cfg default_branch)
  cd "$WORK"
  branch=$(git branch --show-current)
  if [ -z "$branch" ] || [ "$branch" = "$default" ]; then
    echo "the checkout is on '${branch:-a detached HEAD}', not on a work branch"; return 0
  fi
  status running branch="$branch" head="$(git rev-parse HEAD)"
  dirty=$(git status --porcelain)
  if [ -n "$dirty" ]; then
    printf 'the working tree has uncommitted changes:\n%s\n' "$(printf '%s\n' "$dirty" | head -n 20)"; return 0
  fi
  if ! json=$(bounded 120 gh pr view "$branch" --json url,number,state,isDraft,headRefOid 2>&1); then
    printf 'there is no pull request for %s (gh pr view: %s)\n' "$branch" "$(printf '%s' "$json" | tail -n 1)"
    return 0
  fi
  head=$(printf '%s' "$json" | python3 -c 'import json, sys
pr = json.load(sys.stdin)
print(pr.get("headRefOid", ""))
print(" ".join("pr.%s=%s" % (k, str(pr.get(f, "")).lower() if f == "isDraft" else pr.get(f, ""))
               for k, f in (("url", "url"), ("number", "number"), ("state", "state"), ("draft", "isDraft"))))') \
    || { echo "gh pr view answered something unreadable"; return 0; }
  # shellcheck disable=SC2046  # one pr.key=value word per field, none with spaces
  status running $(printf '%s\n' "$head" | sed -n 2p)
  if [ "$(printf '%s\n' "$head" | sed -n 1p)" != "$(git rev-parse HEAD)" ]; then
    echo "the pull request's head is not this checkout's HEAD: commits here are not pushed"; return 0
  fi
}

note() { # note TEXT - the runner's own line in the log, for whoever follows the run
  python3 -c 'import json, sys; print(json.dumps({"type": "runner", "text": sys.argv[1]}))' "$1" >>"$LOG"
}

nudge() { # nudge WHY EXTRA - the prompt that sends a session back to work
  printf 'This run is not finished: %s\n\n' "$1"
  printf 'Continue it from where it stopped, under the same contract. Check the working tree, the plan directory under .dev/ and what is already committed and pushed before acting, then carry the work through to its pull request.\n'
  printf 'If a hard stop of the contract applies, write the reason to %s and end the session.\n' "$RUN_DIR/blocked.md"
  if [ -n "$2" ]; then printf '\nThe operator adds:\n\n%s\n' "$2"; fi
}

# session PROMPT [SESSION-TO-RESUME] - one headless Claude Code session; sets RESULT and SID.
session() {
  local prompt=$1 resume=${2:-} model from=0 rc
  model=$(cfg model)
  SID=${resume:-$(python3 -c 'import uuid; print(uuid.uuid4())')}
  # No agent attribution in commits or PR text: the workflow's commit rules forbid Co-Authored-By lines.
  printf '%s\n' '{"includeCoAuthoredBy": false, "attribution": {"commit": "", "pr": ""}}' >"$RUN_DIR/settings.json"
  local -a args=(-p "$prompt" --plugin-dir "$RUN_DIR/dev" --append-system-prompt-file "$RUN_DIR/contract.md"
    --settings "$RUN_DIR/settings.json" --permission-mode bypassPermissions --disallowed-tools AskUserQuestion
    --output-format stream-json --verbose)
  if [ -n "$model" ]; then args+=(--model "$model"); fi
  if [ -n "$resume" ]; then args+=(--resume "$resume"); else args+=(--session-id "$SID"); fi
  if [ -f "$LOG" ]; then from=$(wc -l <"$LOG"); fi
  status running session="$SID" reason=
  cd "$WORK"
  set +e
  bounded "$(cfg attempt_timeout_seconds)" claude "${args[@]}" >>"$LOG" 2>>"$RUN_DIR/err.log" </dev/null
  rc=$?
  set -e
  local found
  read -r RESULT found <<<"$(outcome "$from")"
  if [ -n "$found" ]; then SID=$found; status running session="$SID"; fi
  if [ "$rc" -eq 124 ] || [ "$rc" -eq 137 ]; then RESULT=timeout; fi
  if [ "$rc" -ne 0 ] && [ "$RESULT" = ok ]; then RESULT="exit-$rc"; fi
}

# work FIRST-PROMPT [SESSION] [EXTRA] - sessions until the run is finished, blocked, or out of attempts.
work() {
  local prompt=$1 sid=${2:-} extra=${3:-} attempt=0 max why pause
  max=$(cfg attempts)
  while :; do
    attempt=$((attempt + 1))
    status running attempt="$attempt"
    # A session whose transcript is gone cannot be resumed: start a new one on the same work.
    if [ -n "$sid" ] && [ ! -f "$PROJECTS/$sid.jsonl" ]; then
      sid=
      prompt="$(cat "$RUN_DIR/prompt.md")

$prompt"
    fi
    session "$prompt" "$sid"
    sid=$SID
    if [ -s "$RUN_DIR/blocked.md" ]; then
      unfinished >/dev/null || true
      status blocked reason="$(head -c 600 "$RUN_DIR/blocked.md")"
      return 0
    fi
    why=$(unfinished)
    if [ -z "$why" ]; then
      status "done" reason=
      return 0
    fi
    if [ "$RESULT" != ok ]; then why="the last session ended early ($RESULT); $why"; fi
    if [ "$attempt" -ge "$max" ]; then
      status failed reason="still unfinished after $attempt sessions: $why"
      return 1
    fi
    status running reason="session $attempt: $why"
    note "session $attempt is not finished: $why"
    if [ "$RESULT" != ok ]; then  # a gateway error or a crash: give it a moment before the next try
      pause=$(( $(cfg backoff_seconds) * attempt ))
      sleep "$(( pause > 600 ? 600 : pause ))"
    fi
    prompt=$(nudge "$why" "$extra")
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

# open_view - a terminal on the instance's desktop that follows this run through view.py, for
# anyone watching over VNC. gnome-terminal hands the window to the desktop session's terminal
# server, so it lives outside this unit and outlasts the run, showing how it ended. A viewer of
# this run is kept (continue); one left by an earlier run is closed. Best effort: a desktop that
# cannot show it never fails the run.
open_view() {
  [ "$(cfg view)" != False ] || return 0
  [ -n "${DISPLAY:-}" ] && command -v gnome-terminal >/dev/null 2>&1 || return 0
  local pid args id
  id=$(cfg id)
  for pid in $(pgrep -u "$(id -u)" -f 'skills/remote/scripts/view\.py' 2>/dev/null || true); do
    args=$(ps -o args= -p "$pid" 2>/dev/null) || continue
    case "$args " in  # only a viewer of this run directory, never a process that merely names the script
      *"/view.py $RUN_DIR $id "*) return 0 ;;
      *"/view.py $RUN_DIR "*) kill "$pid" 2>/dev/null || true ;;
    esac
  done
  if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
    DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    export DBUS_SESSION_BUS_ADDRESS
  fi
  gnome-terminal --maximize --title "dev:remote $id" -- \
    python3 "$RUN_DIR/dev/skills/remote/scripts/view.py" "$RUN_DIR" "$id" >>"$RUN_DIR/view.err" 2>&1 || true
}

setup_env() {
  if [ -r /etc/fleet/computer-use.env ]; then
    set -a
    # shellcheck disable=SC1091
    . /etc/fleet/computer-use.env
    set +a
  fi
  # How long a headless session waits for its background agents before Claude Code ends it.
  # The 600 s default killed a ship fix agent mid-run, so allow ten times that.
  export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS:-6000000}"
  github_env
  trap 'status stopped reason="stopped"; exit 143' TERM INT
  # Any other early exit (set -e) still ends in a recorded state, never a silent "lost".
  trap 'rc=$?; if [ "$rc" -ne 0 ]; then case $(field state) in starting|running)
    status failed reason="runner exited $rc";; esac; fi' EXIT
}

cmd_run() {
  setup_env
  status running id="$(cfg id)" started="$(date -u +%Y-%m-%dT%H:%M:%SZ)" reason=
  open_view
  prepare
  work "$(cat "$RUN_DIR/prompt.md")"
}

cmd_continue() {
  setup_env
  local extra="" why
  [ -d "$WORK/.git" ] || { status failed reason="no checkout at $WORK to continue in"; exit 1; }
  if [ -f "$RUN_DIR/continue.md" ]; then extra=$(cat "$RUN_DIR/continue.md"); fi
  if [ -f "$RUN_DIR/blocked.md" ]; then mv "$RUN_DIR/blocked.md" "$RUN_DIR/blocked-$(date -u +%Y%m%dT%H%M%SZ).md"; fi
  status running continued="$(date -u +%Y-%m-%dT%H:%M:%SZ)" reason=
  open_view
  why=$(unfinished)
  if [ -z "$why" ] && [ -z "$extra" ]; then
    status "done" reason=
    return 0
  fi
  work "$(nudge "${why:-the operator has more work for it}" "$extra")" "$(field session)" "$extra"
}

cmd_state() {
  local active=false
  if systemctl is-active --quiet "$UNIT" 2>/dev/null; then active=true; fi
  python3 - "$RUN_DIR" "$active" <<'PY'
import json, os, sys, time
run_dir, active = sys.argv[1:]
def load(name):
    try:
        return json.load(open(os.path.join(run_dir, name)))
    except (OSError, ValueError):
        return None
try:
    age = round(time.time() - os.path.getmtime(os.path.join(run_dir, "log.jsonl")))
except OSError:
    age = None
print(json.dumps({"status": load("status.json"), "run": load("run.json"), "active": active == "true",
                  "last_event_age": age}))
PY
}

cmd_log() {
  python3 - "$LOG" "${1:-0}" "${2:-40}" <<'PY'
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

# pack - the plan directories this run touched (every one under .dev/ with a file newer than
# the run's start), and the run's own account: status.json, run.json, sent.json, blocked.md.
cmd_pack() {
  python3 - "$RUN_DIR" "$WORK" <<'PY'
import os, sys, tarfile
run_dir, work = sys.argv[1:]
try:
    since = os.path.getmtime(os.path.join(run_dir, "started"))
except OSError:
    since = 0
dev = os.path.join(work, ".dev")
with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as out:
    for name in sorted(os.listdir(dev)) if os.path.isdir(dev) else []:
        folder = os.path.join(dev, name)
        if not os.path.isdir(folder) or os.path.islink(folder):
            continue
        stamps = [os.path.getmtime(os.path.join(root, f)) for root, _dirs, files in os.walk(folder) for f in files]
        if stamps and max(stamps) >= since:
            out.add(folder, arcname="plan/" + name)
    for name in ("status.json", "run.json", "sent.json", "blocked.md", "leftover.patch"):
        if os.path.exists(os.path.join(run_dir, name)):
            out.add(os.path.join(run_dir, name), arcname="run/" + name)
PY
}

cmd_bundle() {
  local branch base
  cd "$WORK"
  branch=$(git branch --show-current)
  base=$(cfg base)
  [ -n "$branch" ] || { echo "the checkout is on a detached HEAD" >&2; exit 1; }
  if [ "$(git rev-parse HEAD)" = "$base" ]; then exit 0; fi  # nothing beyond what the laptop has
  git bundle create -q - "refs/heads/$branch" "^$base"
}

case "${1:-}" in
  run) cmd_run ;;
  continue) cmd_continue ;;
  state) cmd_state ;;
  log) shift; cmd_log "$@" ;;
  pack) cmd_pack ;;
  bundle) cmd_bundle ;;
  *) echo "usage: runner.sh run|continue|state|log OFFSET TAIL|pack|bundle" >&2; exit 2 ;;
esac
