#!/usr/bin/env python3
"""Run a task or a scoped plan to its pull request on a fleet instance you pick.

Usage:
  remote.py start N [--plan NAME] [--task TEXT | --task-file FILE]
  remote.py continue N [--prompt TEXT]
  remote.py status [N] [--json]
  remote.py logs N [--tail LINES] [--follow] [--raw]
  remote.py wait N [--interval SECONDS]
  remote.py stop N
  remote.py pull N
  remote.py doctor

N is the instance: a number (1 is the first one) or its fleet name. An
instance holds one run, so everything after `start` names the instance, not
a run. `start` sends this checkout's HEAD, the plan directory and this plugin
to the instance and starts one unattended yolo session there: with --plan it
builds and ships that plan, with a task it scopes it first. The instance's
runner resumes that session until its pull request is open (or it wrote down
why it cannot be), so a run ends by itself. `continue` puts a run that ended
back to work in its own session and checkout, with more instructions if you
have them; `pull` brings its branch and plan files to this checkout.

This machine keeps no record of a run: its state lives on the instance.
Everything crosses as a stream over `ssh HOST incus exec ws-NAME`.

Configuration lives in ~/.dev-workflow/remote.toml (or $DEV_REMOTE_CONFIG):

  host = "user@fleet-host"      # ssh destination of the fleet host
  instances = []                # fleet names in order (1 is the first); empty = ws1, ws2, ...
  model = ""                    # optional --model for the instance's agent
  attempts = 6                  # sessions a run may take before it ends as failed
  attempt_timeout_hours = 8     # one session's time limit
  view = true                   # a terminal on the instance's desktop follows the run,
                                # for watching over VNC (scripts/view.py)

Exit status: 0 success, 1 a refused or failed step with the reason on
stderr (`wait`: the run did not end done), 2 usage.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: tomli is the same parser, when installed
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

from view import render_event  # noqa: E402 - the stream renderer, shared with the instance's desktop viewer

SKILL_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_ROOT.parents[1]
RUNNER = SKILL_ROOT / "scripts" / "runner.sh"
CONTRACT = SKILL_ROOT / "references" / "unattended.md"

ENDED = ("done", "blocked", "failed", "stopped", "lost")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
WS_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PLUGIN_PARTS = (".claude-plugin", "skills", "references", "scripts")
RUN = "$HOME/.dev-workflow/run"
UNIT = "dev-remote"


class Refused(Exception):
    """A step the script will not take; the message says why and what to do."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def q(text: str) -> str:
    return shlex.quote(text)


# --- configuration -------------------------------------------------------------

def parse_flat_toml(text: str) -> dict:
    """remote.toml's flat subset without a TOML library (Python 3.10): one `key = value` per line,
    the value a string, number, boolean, or array of those, with `#` comments. Anything else
    raises ValueError naming the line."""
    cfg = {}
    for number, raw in enumerate(text.splitlines(), 1):
        line, quote = "", ""
        for char in raw:  # drop a comment that is not inside a string
            if quote:
                quote = "" if char == quote and not line.endswith("\\") else quote
            elif char in "\"'":
                quote = char
            elif char == "#":
                break
            line += char
        line = line.strip()
        if not line:
            continue
        match = re.match(r"^([A-Za-z0-9_-]+)\s*=\s*(.+)$", line)
        if not match:
            raise ValueError(f"line {number}: expected key = value (full TOML needs Python 3.11+ or tomli)")
        value = match.group(2)
        if len(value) > 1 and value[0] == value[-1] == "'":
            cfg[match.group(1)] = value[1:-1]  # a literal string
            continue
        try:
            cfg[match.group(1)] = json.loads(value)
        except ValueError:
            raise ValueError(f"line {number}: unsupported value {value!r} "
                             "(full TOML needs Python 3.11+ or tomli)") from None
    return cfg


def load_config() -> dict:
    path = Path(os.environ.get("DEV_REMOTE_CONFIG", "~/.dev-workflow/remote.toml")).expanduser()
    if not path.exists():
        raise Refused(f"no configuration at {path}; create it with at least: host = \"user@fleet-host\"")
    text = path.read_text(encoding="utf-8")
    try:
        cfg = tomllib.loads(text) if tomllib is not None else parse_flat_toml(text)
    except ValueError as exc:  # tomllib.TOMLDecodeError is a ValueError
        raise Refused(f"{path}: {exc}") from exc
    for key, value in (("transport", "ssh"), ("instances", []), ("model", ""), ("attempts", 6),
                       ("attempt_timeout_hours", 8), ("backoff_seconds", 60), ("view", True)):
        cfg.setdefault(key, value)
    if not isinstance(cfg["view"], bool):
        raise Refused(f"{path}: view must be true or false")
    if cfg["transport"] not in ("ssh", "local"):
        raise Refused(f"{path}: transport must be ssh or local")
    if cfg["transport"] == "ssh" and not cfg.get("host"):
        raise Refused(f"{path}: host is required")
    if not isinstance(cfg["instances"], list) or not all(WS_NAME.match(str(ws)) for ws in cfg["instances"]):
        raise Refused(f"{path}: instances must be a list of fleet names")
    return cfg


def instance(cfg: dict, arg: str) -> str:
    """The fleet name behind an instance argument: its number, or the name itself."""
    arg = str(arg).strip()
    if arg.isdigit():
        number, names = int(arg), cfg["instances"]
        if number < 1 or (names and number > len(names)):
            raise Refused(f"no instance {number}" + (f" (instances: 1 to {len(names)})" if names else ""))
        return names[number - 1] if names else f"ws{number}"
    name = arg[3:] if arg.startswith("ws-") else arg
    if not WS_NAME.match(name):
        raise Refused(f"{arg!r} is not an instance: give its number (1 is the first) or its fleet name")
    return name


def label(cfg: dict, ws: str) -> str:
    """How an instance is addressed in a command: its number where it has one."""
    if ws in cfg["instances"]:
        return str(cfg["instances"].index(ws) + 1)
    match = re.match(r"^ws(\d+)$", ws)
    return match.group(1) if match and not cfg["instances"] else ws


# --- local git -----------------------------------------------------------------

def git(*args: str, cwd: Path | None = None, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise Refused(f"git {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout.strip() if result.returncode == 0 else ""


def git_ok(*args: str, cwd: Path | None = None) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=False).returncode == 0


def default_branch(root: Path) -> str:
    head = git("symbolic-ref", "-q", "--short", "refs/remotes/origin/HEAD", cwd=root, check=False)
    if head.startswith("origin/"):
        return head[len("origin/"):]
    for name in ("main", "master"):
        if git_ok("rev-parse", "--verify", "-q", f"refs/remotes/origin/{name}", cwd=root):
            return name
    for name in ("main", "master"):
        if git_ok("rev-parse", "--verify", "-q", f"refs/heads/{name}", cwd=root):
            return name
    return "main"


def github_repo(url: str) -> str:
    """OWNER/REPO for a github.com remote URL in any of its forms, else empty."""
    match = re.match(r"^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$",
                     url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def checked_out_at(root: Path, branch: str) -> Path | None:
    current = None
    for line in git("worktree", "list", "--porcelain", cwd=root).splitlines():
        if line.startswith("worktree "):
            current = Path(line[len("worktree "):])
        elif line == f"branch refs/heads/{branch}" and current is not None:
            return current
    return None


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(folder: Path, prefix: str = "") -> dict[str, str]:
    return {prefix + p.relative_to(folder).as_posix(): sha256(p) for p in sorted(folder.rglob("*")) if p.is_file()}


def repo_root() -> Path:
    root = git("rev-parse", "--show-toplevel", check=False)
    if not root:
        raise Refused("run this from inside the repository's checkout")
    return Path(root)


# --- the fleet host ------------------------------------------------------------

class Host:
    """Runs scripts on the fleet host and, through incus, inside a workspace."""

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.local = cfg["transport"] == "local"

    def sh(self, script: str, *, data: bytes | None = None, check: bool = True,
           timeout: float | None = 600) -> subprocess.CompletedProcess:
        script = 'export PATH="$HOME/.local/bin:$PATH"; ' + script
        if self.local:
            argv = ["bash", "-c", script]
        else:
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=30",
                    self.cfg["host"], "bash -c " + q(script)]
        try:
            result = subprocess.run(argv, input=data, capture_output=True, check=False, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise Refused(f"the fleet host did not answer within {timeout:.0f}s") from exc
        if check and result.returncode != 0:
            err = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            where = "the fleet host" if self.local else self.cfg["host"]
            raise Refused(f"on {where}: {err or f'exit {result.returncode}'}")
        return result

    def text(self, script: str, **kwargs) -> str:
        return self.sh(script, **kwargs).stdout.decode(errors="replace").strip()

    def vm(self, ws: str, script: str, **kwargs) -> subprocess.CompletedProcess:
        """Run a script as the workspace's agent user, in its login environment."""
        return self.sh(f"incus exec ws-{ws} -- runuser -l agent -c {q(script)}", **kwargs)

    def vm_text(self, ws: str, script: str, **kwargs) -> str:
        return self.vm(ws, script, **kwargs).stdout.decode(errors="replace").strip()

    def instances(self) -> dict[str, str]:
        rows = json.loads(self.text("incus list --format json") or "[]")
        return {r["name"][3:]: r.get("status", "?") for r in rows if r.get("name", "").startswith("ws-")}

    def runner(self, ws: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
        return self.vm(ws, f'"{RUN}/runner.sh" ' + " ".join(q(a) for a in args), **kwargs)

    def start_unit(self, ws: str, home: str, command: str) -> None:
        script = q(f"{home}/.dev-workflow/run/runner.sh") + " " + command
        self.sh(f"incus exec ws-{q(ws)} -- systemctl reset-failed {UNIT} 2>/dev/null; "
                f"incus exec ws-{q(ws)} -- systemd-run --quiet --collect --unit={UNIT} --uid=agent "
                f"/bin/bash -lc {q(script)}")


def ready(host: Host, ws: str) -> None:
    states = host.instances()
    if ws not in states:
        raise Refused(f"no instance ws-{ws} on the host (have: {', '.join(sorted(states)) or 'none'})")
    if states[ws] != "Running":
        host.sh(f"fleet start {q(ws)}")


def run_state(host: Host, ws: str) -> dict:
    """The instance's run: its status.json with run.json's facts and the unit's liveness folded in.

    state is idle (no run here), starting, running, done, blocked, failed, stopped, or lost
    (the runner is gone without recording an end: killed, crashed, or the instance restarted).
    """
    result = host.vm(ws, f'if [ -x "{RUN}/runner.sh" ]; then "{RUN}/runner.sh" state; else echo "{{}}"; fi',
                     check=False)
    try:
        info = json.loads(result.stdout.decode(errors="replace"))
    except ValueError:
        detail = result.stderr.decode(errors="replace").strip()[-300:]
        raise Refused(f"instance {ws} did not answer: {detail or 'no output'}") from None
    status, run = info.get("status") or {}, info.get("run") or {}
    if not run:
        return {"state": "idle"}
    state = status.get("state", "starting")
    if state in ("starting", "running") and not info.get("active"):
        state = "lost"
    return {**{k: run.get(k) for k in ("id", "repo", "plan", "base", "origin_url")},
            "branch": run.get("branch"), **status, "state": state, "last_event_age": info.get("last_event_age")}


def live_run(host: Host, cfg: dict, ws: str) -> dict:
    live = run_state(host, ws)
    if live["state"] == "idle":
        raise Refused(f"instance {label(cfg, ws)} has no run")
    return live


# --- what crosses --------------------------------------------------------------

def merge_plan(remote: Path, local: Path, dispatched: dict[str, str]) -> dict:
    """Copy the instance's plan files home without losing an edit made here.

    `dispatched` is the common ground: each file's hash when both sides last agreed. A file
    this machine left untouched since then takes the instance's version; one edited on both
    sides keeps the local version and gets the instance's beside it as {name}.remote.
    """
    updated, conflicts = [], []
    local.mkdir(parents=True, exist_ok=True)
    for src in sorted(p for p in remote.rglob("*") if p.is_file()):
        rel = src.relative_to(remote).as_posix()
        dst = local / rel
        theirs = sha256(src)
        if dst.exists() and sha256(dst) == theirs:
            continue
        mine_changed = dst.exists() and sha256(dst) != dispatched.get(rel)
        theirs_changed = theirs != dispatched.get(rel)
        if mine_changed and theirs_changed:
            side = dst.with_name(dst.name + ".remote")
            shutil.copy2(src, side)
            conflicts.append(rel)
        elif theirs_changed:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            updated.append(rel)
    return {"updated": updated, "conflicts": conflicts}


# tarfile's extraction filters arrived in 3.12 and were backported to 3.10.12 and 3.11.4.
HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


def extract_tar(data: bytes, dest: Path) -> None:
    """Unpack the instance's output, which is untrusted: nothing escapes dest, nothing but data."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        if HAS_DATA_FILTER:
            archive.extractall(dest, filter="data")
            return
        root = dest.resolve()
        members = archive.getmembers()
        for member in members:  # every path is checked before anything is written
            target = (dest / member.name).resolve()
            if os.path.isabs(member.name) or (target != root and root not in target.parents):
                raise Refused(f"the instance's output holds a path outside its folder: {member.name!r}")
        for member in members:
            if not (member.isfile() or member.isdir()):
                continue  # links, devices, and fifos are not data
            # As the data filter does: no setuid or setgid bits, and nothing writable by others.
            member.mode = (member.mode & 0o755) | (0o700 if member.isdir() else 0o600)
            archive.extract(member, dest)


def plugin_tar(stage: tarfile.TarFile, arcroot: str) -> None:
    def keep(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if "__pycache__" in parts or info.name.endswith((".pyc", ".DS_Store")):
            return None
        return info
    for part in PLUGIN_PARTS:
        if (PLUGIN_ROOT / part).exists():
            stage.add(PLUGIN_ROOT / part, arcname=f"{arcroot}/{part}", filter=keep)
    if not (PLUGIN_ROOT / ".claude-plugin" / "plugin.json").exists():
        # Dispatched from a generated distribution (Codex): the sandbox runs Claude Code, which
        # namespaces the skills by this manifest's name, so `/dev:build` keeps resolving.
        add_bytes(stage, f"{arcroot}/.claude-plugin/plugin.json", json.dumps({"name": "dev"}).encode())


def add_bytes(stage: tarfile.TarFile, name: str, data: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    stage.addfile(info, io.BytesIO(data))


def program_files(stage: tarfile.TarFile) -> None:
    """The plugin and the runner: sent with every start and continue, so a fix here reaches a run."""
    plugin_tar(stage, "dev")
    add_bytes(stage, "runner.sh", RUNNER.read_bytes(), 0o755)


def bundle_for(root: Path, sent: str, have: list[str]) -> bytes | None:
    """A git bundle of HEAD without what the instance already has; None when it has HEAD."""
    known = [sha for sha in have if git_ok("cat-file", "-e", f"{sha}^{{commit}}", cwd=root)]
    if sent in known:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.bundle"
        git("bundle", "create", "-q", str(path), "HEAD", *[f"^{sha}" for sha in known], cwd=root)
        return path.read_bytes()


def slug(text: str, limit: int = 32) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "task"


def latest_review(plan_dir: Path) -> tuple[str | None, str | None]:
    reviews = sorted(plan_dir.glob("review_*.md"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    if not reviews:
        return None, None
    match = re.search(r"^\**Verdict:?\**:?\s*(.+)$", reviews[-1].read_text(encoding="utf-8"), re.MULTILINE)
    return str(reviews[-1]), match.group(1).strip() if match else None


def open_calls(pr_md: Path) -> list[str]:
    if not pr_md.exists():
        return []
    text = pr_md.read_text(encoding="utf-8")
    section = re.split(r"^## Open calls\s*$", text, flags=re.MULTILINE)
    if len(section) < 2:
        return []
    body = re.split(r"^## ", section[1], flags=re.MULTILINE)[0]
    calls: list[str] = []
    for line in (raw.strip() for raw in body.splitlines()):
        if not line:
            continue
        if re.match(r"^([-*]|\d+\.)\s", line) or not calls:
            calls.append(line)
        else:  # a wrapped continuation of the previous call
            calls[-1] += " " + line
    return [] if [c.lower().strip("-* .") for c in calls] == ["none"] else calls


# --- commands ------------------------------------------------------------------

def cmd_start(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    root = repo_root()
    repo = root.name
    if not SAFE_NAME.match(repo):
        raise Refused(f"repository folder name {repo!r} is not usable as a checkout name")
    task = Path(args.task_file).read_text(encoding="utf-8").strip() if args.task_file else (args.task or "").strip()
    plan_dir = None
    if args.plan:
        plan_dir = root / ".dev" / args.plan
        if not SAFE_NAME.match(args.plan) or not (plan_dir / "spec.md").is_file():
            raise Refused(f"no scoped plan at .dev/{args.plan}/spec.md; scope it first, or pass the work as --task")
        prompt = (f"/dev:yolo Build and ship the plan in `.dev/{args.plan}/`. Its scope stage is done: `spec.md` "
                  "is settled, so start at build, which continues from whatever `implementation-notes.md` already "
                  "records, and go on through ship to the pull request.")
        if task:
            prompt += f"\n\nThe operator adds:\n\n{task}"
    elif task:
        prompt = f"/dev:yolo {task}"
    else:
        raise Refused("nothing to run: pass --plan NAME, --task TEXT, or --task-file FILE")

    name = args.plan or slug(task.splitlines()[0])
    default = default_branch(root)
    current = git("branch", "--show-current", cwd=root, check=False)
    branch = current if current and current != default else f"dev/{name}"
    sent = git("rev-parse", "HEAD", cwd=root)
    base_ref = next((ref for ref in (f"refs/remotes/origin/{default}", f"refs/heads/{default}")
                     if git_ok("rev-parse", "--verify", "-q", ref, cwd=root)), None)
    base = git("merge-base", "HEAD", base_ref, cwd=root) if base_ref else sent
    origin_url = git("remote", "get-url", "origin", cwd=root, check=False)
    not_sent = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True,
                              check=False).stdout.splitlines()

    host = Host(cfg)
    ready(host, ws)
    live = run_state(host, ws)
    if live["state"] in ("starting", "running"):
        n = label(cfg, ws)
        raise Refused(f"instance {n} is running {live.get('id')} ({live.get('repo')}, {live.get('branch')}); "
                      f"pick another instance, or `stop {n}` first")
    info = host.vm_text(ws, f'printf "%s\\n" "$HOME"; git -C "$HOME/work/"{q(repo)} for-each-ref '
                            "--format='%(objectname)' 2>/dev/null | sort -u | head -n 400").splitlines()
    home, have = info[0], info[1:]
    run = f"{name}-{datetime.now(timezone.utc).strftime('%y%m%d-%H%M')}-{os.urandom(2).hex()}"
    payload = {"id": run, "repo": repo, "plan": args.plan or "", "branch": branch, "sent": sent, "base": base,
               "default_branch": default, "origin_url": origin_url, "github_repo": github_repo(origin_url),
               "model": cfg["model"], "attempts": int(cfg["attempts"]), "view": cfg["view"],
               "attempt_timeout_seconds": int(float(cfg["attempt_timeout_hours"]) * 3600),
               "backoff_seconds": int(cfg["backoff_seconds"]), "started": now()}
    contract = CONTRACT.read_text(encoding="utf-8")
    for key, value in (("{plugin}", f"{home}/.dev-workflow/run/dev"), ("{run}", f"{home}/.dev-workflow/run"),
                       ("{branch}", branch), ("{default}", default)):
        contract = contract.replace(key, value)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stage:
        program_files(stage)
        add_bytes(stage, "run.json", json.dumps(payload, indent=2).encode())
        add_bytes(stage, "prompt.md", prompt.encode())
        add_bytes(stage, "contract.md", contract.encode())
        add_bytes(stage, "sent.json", json.dumps(manifest(plan_dir, f"{args.plan}/") if plan_dir else {}).encode())
        if plan_dir:
            stage.add(plan_dir, arcname=f"plan/{args.plan}")
        bundle = bundle_for(root, sent, have)
        if bundle is not None:
            add_bytes(stage, "in.bundle", bundle)
    # The run before this one moves to history (the newest ten are kept); status.json says
    # "starting" before the unit exists, so nobody reads an old ending as this run's.
    host.vm(ws, f'set -e; d="{RUN}"; h="$HOME/.dev-workflow/history"; mkdir -p "$h"\n'
                'if [ -f "$d/run.json" ]; then old=$(python3 -c \'import json, sys; '
                'print(json.load(open(sys.argv[1])).get("id") or "run")\' "$d/run.json"); '
                'rm -rf "$h/$old"; mv "$d" "$h/$old"; else rm -rf "$d"; fi\n'
                'mkdir -p "$d"; tar -xzf - -C "$d"; touch "$d/started"\n'
                f'printf \'{{"state": "starting", "id": "%s"}}\\n\' {q(run)} >"$d/status.json"\n'
                '(cd "$h" && ls -1t | tail -n +11 | while read -r gone; do rm -rf -- "$gone"; done)',
            data=buffer.getvalue())
    host.start_unit(ws, home, "run")
    view = host.text(f"fleet view {q(ws)}", check=False)
    print(json.dumps({"instance": label(cfg, ws), "workspace": ws, "run": run, "repo": repo, "branch": branch,
                      "sent": sent, "base": base, "plan": args.plan, "view": view or None,
                      "not_sent": not_sent}, indent=2))
    return 0


def cmd_continue(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    host = Host(cfg)
    ready(host, ws)
    live = live_run(host, cfg, ws)
    if live["state"] in ("starting", "running"):
        raise Refused(f"instance {label(cfg, ws)} is still running {live.get('id')}; `stop` it first to redirect it")
    home = host.vm_text(ws, 'printf %s "$HOME"')
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as stage:
        program_files(stage)
        if args.prompt:
            add_bytes(stage, "continue.md", args.prompt.strip().encode())
    host.vm(ws, f'set -e; d="{RUN}"; rm -rf "$d/dev" "$d/continue.md"; tar -xzf - -C "$d"\n'
                "python3 -c 'import json, sys\n"
                "data = json.load(open(sys.argv[1]))\n"
                "data.update(state=\"starting\", reason=\"\")\n"
                "json.dump(data, open(sys.argv[1], \"w\"), indent=2)' \"$d/status.json\"",
            data=buffer.getvalue())
    host.start_unit(ws, home, "continue")
    print(json.dumps({"instance": label(cfg, ws), "workspace": ws, "run": live.get("id"), "was": live["state"],
                      "session": live.get("session"), "prompt": args.prompt or None}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    host = Host(cfg)
    if args.instance:
        names = [instance(cfg, args.instance)]
    else:
        states = host.instances()
        names = cfg["instances"] or sorted(states)
    rows = []
    for ws in names:
        try:
            live = run_state(host, ws)
        except Refused as exc:
            live = {"state": "unreachable", "reason": str(exc)}
        rows.append({"instance": label(cfg, ws), "workspace": ws, **live})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'#':<4} {'STATE':<9} {'RUN':<36} {'SESSIONS':<8} {'IDLE':<6} DETAIL")
    for r in rows:
        age = r.get("last_event_age")
        pr = (r.get("pr") or {}).get("url")
        detail = " · ".join(str(x) for x in (r.get("branch"), pr, " ".join(str(r.get("reason") or "").split())[:160])
                            if x)
        print(f"{r['instance']:<4} {r['state']:<9} {r.get('id') or '-':<36} {str(r.get('attempt') or '-'):<8} "
              f"{'-' if age is None else f'{int(age // 60)}m':<6} {detail}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    host = Host(cfg)
    offset = None
    while True:
        # Most raw lines are tool results that render to nothing, so --tail counts rendered lines.
        raw_tail = args.tail if args.raw else args.tail * 3
        out = host.runner(ws, "log", str(offset or 0), str(raw_tail), check=False)
        lines = out.stdout.decode(errors="replace").splitlines()
        header, body = (lines[0], lines[1:]) if lines and lines[0].startswith("#") else ("", lines)
        shown = body if args.raw else [text for line in body for text in render_event(line)]
        for text in shown if offset else shown[-args.tail:]:
            print(text)
        if header:
            offset = int(header.split()[-1])
        if not args.follow:
            return 0
        state = run_state(host, ws)["state"]
        if state in ENDED or state == "idle":
            print(f"-- run {state}")
            return 0
        sys.stdout.flush()
        time.sleep(5)


def cmd_wait(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    host = Host(cfg)
    seen, failures = None, 0
    while True:
        try:
            live = live_run(host, cfg, ws)
            failures = 0
        except Refused as exc:
            failures += 1
            if failures >= args.max_failures or "has no run" in str(exc):
                raise Refused(f"gave up waiting: {exc}") from exc
            time.sleep(args.interval)
            continue
        marker = (live["state"], live.get("attempt"), live.get("reason"))
        if marker != seen:
            note = " ".join(str(live.get("reason") or "").split())[:200]
            print(f"{now()} {live.get('id')}: {live['state']} (session {live.get('attempt') or '-'})"
                  f"{' - ' + note if note else ''}", flush=True)
            seen = marker
        if live["state"] in ENDED:
            return 0 if live["state"] == "done" else 1
        time.sleep(args.interval)


def cmd_stop(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    host = Host(cfg)
    host.sh(f"incus exec ws-{q(ws)} -- systemctl stop {UNIT}", check=False)
    print(json.dumps({"instance": label(cfg, ws), **run_state(host, ws)}, indent=2))
    return 0


def update_branch(root: Path, branch: str, ref: str) -> str:
    """Move the local branch to the instance's commits by fast-forward only; says what happened."""
    local = f"refs/heads/{branch}"
    if not git_ok("rev-parse", "--verify", "-q", local, cwd=root):
        git("branch", branch, ref, cwd=root)
        return "created"
    if git("rev-parse", local, cwd=root) == git("rev-parse", ref, cwd=root):
        return "current"
    if git_ok("merge-base", "--is-ancestor", ref, local, cwd=root):
        return "ahead"  # this machine already has those commits and more
    if not git_ok("merge-base", "--is-ancestor", local, ref, cwd=root):
        return f"diverged: the instance's commits are at {ref}"
    checkout = checked_out_at(root, branch)
    if checkout is None:
        git("update-ref", local, ref, cwd=root)
        return "fast-forwarded"
    if git("status", "--porcelain", "--untracked-files=no", cwd=checkout):
        return f"not updated: {checkout} has uncommitted changes; the instance's commits are at {ref}"
    git("merge", "-q", "--ff-only", ref, cwd=checkout)
    return "fast-forwarded"


def cmd_pull(args: argparse.Namespace) -> int:
    cfg = load_config()
    ws = instance(cfg, args.instance)
    host = Host(cfg)
    live = live_run(host, cfg, ws)
    root = repo_root()
    if root.name != live.get("repo"):
        raise Refused(f"instance {label(cfg, ws)} holds a run of {live.get('repo')}; run pull from that checkout")
    branch, base, ref = live.get("branch") or "", live.get("base") or "", f"refs/dev-remote/{ws}"
    summary = {"instance": label(cfg, ws), "run": live.get("id"), "state": live["state"],
               "reason": live.get("reason") or None, "pr": live.get("pr") or None, "branch": branch}
    with tempfile.TemporaryDirectory() as tmp:
        bundle = host.runner(ws, "bundle").stdout
        if bundle:
            path = Path(tmp) / "in.bundle"
            path.write_bytes(bundle)
            git("fetch", "-q", str(path), f"+refs/heads/{branch}:{ref}", cwd=root)
            summary["branch_update"] = update_branch(root, branch, ref)
            summary["commits"] = git("log", "--oneline", "--reverse", f"{base}..{ref}", cwd=root).splitlines()
        else:
            summary["branch_update"], summary["commits"] = "no commits beyond what was sent", []
        out = Path(tmp) / "out"
        extract_tar(host.runner(ws, "pack").stdout, out)
        try:
            sent = json.loads((out / "run" / "sent.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sent = {}
        plans = out / "plan"
        summary["plan"] = merge_plan(plans, root / ".dev", sent) if plans.is_dir() else {"updated": [], "conflicts": []}
        if plans.is_dir():
            # What is now the same on both sides is the new common ground for the next pull.
            for rel, theirs in manifest(plans).items():
                mine = root / ".dev" / rel
                if mine.is_file() and sha256(mine) == theirs:
                    sent[rel] = theirs
            host.vm(ws, f'cat >"{RUN}/sent.json"', data=json.dumps(sent).encode())
        blocked = out / "run" / "blocked.md"
        summary["blocked"] = blocked.read_text(encoding="utf-8").strip() if blocked.is_file() else None
        names = sorted(p.name for p in plans.iterdir() if p.is_dir()) if plans.is_dir() else []
    summary["plans"] = names
    for name in names:
        review, verdict = latest_review(root / ".dev" / name)
        if review or (root / ".dev" / name / "pr.md").exists():
            summary["review"], summary["verdict"] = review, verdict
            summary["open_calls"] = open_calls(root / ".dev" / name / "pr.md")
    print(json.dumps(summary, indent=2))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    host = Host(cfg)
    report = {"host": cfg.get("host", "local"), "instances": {}}
    states = host.instances()
    for ws in cfg["instances"] or sorted(states):
        entry = {"workspace": ws, "status": states.get(ws, "missing")}
        if entry["status"] == "Running":
            gh = ("if ! command -v gh >/dev/null; then echo missing; "
                  "elif timeout 30 gh auth status >/dev/null 2>&1; then echo signed-in; else echo signed-out; fi")
            info = host.vm_text(ws, f'echo "claude=$(claude --version 2>/dev/null | head -1)"; echo "gh=$({gh})"',
                                check=False)
            fields = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
            entry["claude"] = fields.get("claude") or "missing"
            entry["gh"] = fields.get("gh") or "missing"
        report["instances"][label(cfg, ws)] = entry
    usable = [e for e in report["instances"].values()
              if e["status"] == "Running" and e["claude"] != "missing" and e["gh"] == "signed-in"]
    report["ok"] = bool(usable)
    print(json.dumps(report, indent=2))
    return 0 if usable else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("start")
    s.add_argument("instance")
    s.add_argument("--plan")
    what = s.add_mutually_exclusive_group()
    what.add_argument("--task")
    what.add_argument("--task-file")
    c = sub.add_parser("continue")
    c.add_argument("instance")
    c.add_argument("--prompt", help="what the session should do next")
    st = sub.add_parser("status")
    st.add_argument("instance", nargs="?")
    st.add_argument("--json", action="store_true")
    lg = sub.add_parser("logs")
    lg.add_argument("instance")
    lg.add_argument("--tail", type=int, default=40)
    lg.add_argument("--follow", action="store_true")
    lg.add_argument("--raw", action="store_true")
    w = sub.add_parser("wait")
    w.add_argument("instance")
    w.add_argument("--interval", type=float, default=60)
    w.add_argument("--max-failures", type=int, default=30)
    for name in ("stop", "pull"):
        sub.add_parser(name).add_argument("instance")
    sub.add_parser("doctor")
    args = parser.parse_args(argv)
    handler = {"start": cmd_start, "continue": cmd_continue, "status": cmd_status, "logs": cmd_logs,
               "wait": cmd_wait, "stop": cmd_stop, "pull": cmd_pull, "doctor": cmd_doctor}[args.command]
    try:
        return handler(args)
    except Refused as exc:
        print(f"remote: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
