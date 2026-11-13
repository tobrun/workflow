#!/usr/bin/env python3
"""Run build and ship for a scoped plan in a sandbox workspace on a fleet host.

Usage:
  remote.py doctor
  remote.py dispatch --plan NAME [--phases build,ship] [--workspace WS] [--branch B]
                    [--publish auto|sandbox|laptop]
  remote.py status [RUN] [--all] [--json]
  remote.py logs RUN [--tail N] [--follow] [--raw]
  remote.py wait RUN [--interval SECONDS]
  remote.py stop RUN
  remote.py resume RUN [--continue PHASE | --from PHASE] [--prompt TEXT] [--phases LIST]
                     [--workspace WS] [--publish auto|sandbox|laptop]
  remote.py collect RUN [--keep-lock]
  remote.py release RUN

The laptop keeps planning and MCP access; the sandbox gets the committed
branch, the plan directory and this plugin, and runs each phase as a headless
Claude Code session under references/unattended.md. Any run that is not
running can resume, collected or not, from a phase's own session or from a
fresh start of a phase. Ship publishes from the
sandbox when its git credentials can push to origin, otherwise from here
after collect. Every byte
crosses through the fleet host: git objects through its exchange clone
(`fleet sync` in, `fleet fetch` out), everything else as tar streams over
`incus exec`. references/protocol.md owns the boundaries and the run states.

Configuration lives in ~/.dev-workflow/remote.toml (or $DEV_REMOTE_CONFIG):

  host = "user@fleet-host"            # ssh destination of the fleet host
  sources_root = "/srv/agent-sources" # the host's exchange clones (fleet sourcesRoot)
  workspaces = []                     # fleet names to use; empty = every running ws-*
  phase_timeout_hours = 8
  model = ""                          # optional --model for the sandbox agent
  publish = "auto"                    # who pushes and opens the PR: auto (the sandbox
                                      # when its credentials pass the probe), sandbox, laptop
  view = true                         # open a terminal on the sandbox desktop that follows
                                      # the run live (scripts/view.py), for watching over VNC

Run records and collected artifacts live under ~/.dev-workflow/remote/
(or $DEV_REMOTE_HOME). Exit status: 0 success, 1 a refused or failed step
with the reason on stderr, 2 usage.
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

from view import render_event  # noqa: E402 - the stream renderer, shared with the sandbox desktop viewer

SKILL_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = SKILL_ROOT.parents[1]
RUNNER = SKILL_ROOT / "scripts" / "runner.sh"
CONTRACT = SKILL_ROOT / "references" / "unattended.md"
LINT_SPEC = PLUGIN_ROOT / "skills" / "scope" / "scripts" / "lint-spec.py"

PHASES = ("build", "ship", "ship-quick")
PUBLISH = ("auto", "sandbox", "laptop")
TERMINAL = ("done", "failed", "stopped", "lost")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")
WS_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
PLUGIN_PARTS = (".claude-plugin", "skills", "references", "scripts")
LOCK = "$HOME/.dev-workflow/lock"


class Refused(Exception):
    """A step the script will not take; the message says why and what to do."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def q(text: str) -> str:
    return shlex.quote(text)


# --- configuration and local state -------------------------------------------

def state_root() -> Path:
    return Path(os.environ.get("DEV_REMOTE_HOME", "~/.dev-workflow/remote")).expanduser()


def tmp_root() -> Path:
    return Path(os.environ.get("DEV_REMOTE_TMP", "/tmp"))


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
    cfg.setdefault("transport", "ssh")
    cfg.setdefault("sources_root", "/srv/agent-sources")
    cfg.setdefault("workspaces", [])
    cfg.setdefault("phase_timeout_hours", 8)
    cfg.setdefault("model", "")
    cfg.setdefault("publish", "auto")
    cfg.setdefault("view", True)
    if cfg["publish"] not in PUBLISH:
        raise Refused(f"{path}: publish must be one of {', '.join(PUBLISH)}")
    if not isinstance(cfg["view"], bool):
        raise Refused(f"{path}: view must be true or false")
    if cfg["transport"] not in ("ssh", "local"):
        raise Refused(f"{path}: transport must be ssh or local")
    if cfg["transport"] == "ssh" and not cfg.get("host"):
        raise Refused(f"{path}: host is required")
    for ws in cfg["workspaces"]:
        if not WS_NAME.match(str(ws)):
            raise Refused(f"{path}: invalid workspace name {ws!r}")
    return cfg


def record_path(run: str) -> Path:
    return state_root() / "runs" / f"{run}.json"


def load_record(run: str) -> dict:
    path = record_path(run)
    if not SAFE_NAME.match(run) or not path.exists():
        known = ", ".join(r["id"] for r in all_records()) or "none"
        raise Refused(f"no run {run!r} (known: {known})")
    return json.loads(path.read_text(encoding="utf-8"))


def save_record(rec: dict) -> None:
    path = record_path(rec["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def all_records() -> list[dict]:
    folder = state_root() / "runs"
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.json")):
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except ValueError:
            continue
    return sorted(rows, key=lambda r: r.get("dispatched_at", ""))


# --- local git ---------------------------------------------------------------

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


def normalize_url(url: str) -> str:
    url = url.strip().lower()
    url = re.sub(r"^(ssh://)?git@([^:/]+)[:/]", r"\2/", url)
    url = re.sub(r"^[a-z+]+://", "", url)
    return re.sub(r"\.git/?$", "", url).rstrip("/")


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


def manifest(folder: Path) -> dict[str, str]:
    return {p.relative_to(folder).as_posix(): sha256(p) for p in sorted(folder.rglob("*")) if p.is_file()}


# --- the fleet host ----------------------------------------------------------

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

    def git_url(self, path: str) -> str:
        if self.local:
            return path
        return f"ssh://{self.cfg['host']}{path}"

    def instances(self) -> dict[str, str]:
        rows = json.loads(self.text("incus list --format json") or "[]")
        return {r["name"][3:]: r.get("status", "?") for r in rows if r.get("name", "").startswith("ws-")}


def run_dir(rec: dict) -> str:
    return f"{rec['vm_home']}/.dev-workflow/runs/{rec['id']}"


def runner(host: Host, rec: dict, *args: str, **kwargs) -> subprocess.CompletedProcess:
    return host.vm(rec["workspace"], " ".join(q(a) for a in [f"{run_dir(rec)}/runner.sh", *args]), **kwargs)


def acquire(host: Host, cfg: dict, wanted: str | None, run: str, prefer: str | None = None) -> str:
    states = host.instances()
    if wanted:
        if wanted not in states:
            raise Refused(f"no workspace {wanted!r} on the host (have: {', '.join(sorted(states)) or 'none'})")
        candidates = [wanted]
    else:
        allowed = set(cfg["workspaces"]) or set(states)
        candidates = sorted(ws for ws, status in states.items() if status == "Running" and ws in allowed)
        if prefer in candidates:  # a resumed run goes back where its caches and transcripts are
            candidates.remove(prefer)
            candidates.insert(0, prefer)
    held = []
    for ws in candidates:
        if states.get(ws) != "Running":
            host.sh(f"fleet start {q(ws)}")
        out = host.vm_text(ws, f'mkdir -p "$HOME/.dev-workflow" && if mkdir "{LOCK}" 2>/dev/null; '
                               f'then printf %s {q(run)} >"{LOCK}/run" && echo acquired; '
                               f'else echo "held:$(cat "{LOCK}/run" 2>/dev/null)"; fi')
        if out == "acquired":
            return ws
        held.append(f"{ws} ({out.removeprefix('held:') or 'unknown run'})")
    if not candidates:
        raise Refused("no running workspace to use; start one with `fleet start <name>` on the host")
    raise Refused("every workspace is busy: " + ", ".join(held)
                  + "; collect or release a finished run first (remote.py status)")


def release(host: Host, rec: dict) -> bool:
    out = host.vm_text(rec["workspace"], f'if [ "$(cat "{LOCK}/run" 2>/dev/null)" = {q(rec["id"])} ]; '
                                         f'then rm -rf "{LOCK}" && echo released; fi', check=False)
    return out == "released"


def run_state(host: Host, rec: dict) -> dict:
    """The run's status.json, with the unit's liveness folded into `state`."""
    result = runner(host, rec, "state", check=False)
    try:
        info = json.loads(result.stdout.decode(errors="replace"))
    except ValueError:
        return {"state": "unknown", "detail": result.stderr.decode(errors="replace").strip()[-300:]}
    status = info.get("status") or {}
    state = status.get("state", "starting")
    if state in ("starting", "running") and not info.get("active"):
        state = "lost"  # the unit is gone but never recorded an end: killed, crashed, or the VM restarted
    status["state"] = state
    status["last_event_age"] = info.get("last_event_age")
    return status


# --- plan files --------------------------------------------------------------

def merge_plan(remote: Path, local: Path, dispatched: dict[str, str]) -> dict:
    """Copy the sandbox's plan files home without losing an edit made here.

    A file this machine left untouched since dispatch takes the sandbox's
    version; one edited on both sides keeps the local version and gets the
    sandbox's beside it as {name}.remote.
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


def extract_tar(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(dest, filter="data")  # sandbox output is untrusted: no escapes, no devices


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


def probe_publish(host: Host, ws: str, repo: str, branch: str, origin_url: str, run: str) -> str:
    """Empty when the workspace can push to origin and `gh` is signed in, else what failed.

    A dry-run push of a ref that does not exist authenticates for receive-pack
    without creating anything, so it proves write access whatever supplies the
    credential (a token, a credential helper, a host relay).
    """
    if not origin_url:
        return "this checkout has no origin"
    script = (
        f"cd \"$HOME/work/\"{q(repo)} || exit 3\n"
        "export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes'\n"
        "bounded() { if command -v timeout >/dev/null; then timeout 60 \"$@\"; else \"$@\"; fi; }\n"
        f"if ! out=$(bounded git push --dry-run --no-verify -q {q(origin_url)} "
        f"{q(f'refs/remotes/origin/{branch}:refs/heads/dev-remote-probe/{run}')} 2>&1); then\n"
        "  echo \"git push to origin failed: $(printf %s \"$out\" | tail -n 1)\"; exit 1\n"
        "fi\n"
        "command -v gh >/dev/null || { echo 'gh is not installed'; exit 1; }\n"
        "bounded gh auth status >/dev/null 2>&1 || { echo 'gh is not signed in'; exit 1; }\n")
    result = host.vm(ws, script, check=False)
    if result.returncode == 0:
        return ""
    return result.stdout.decode(errors="replace").strip() or f"probe exited {result.returncode}"


def resolve_publish(wanted: str, phases: list[str], probe) -> dict:
    """The run's publication mode, with the reason when it is not the sandbox."""
    if not any(p.startswith("ship") for p in phases):
        return {"mode": "laptop", "reason": "no ship phase"}
    if wanted == "laptop":
        return {"mode": "laptop", "reason": "requested"}
    failure = probe()
    if not failure:
        return {"mode": "sandbox"}
    if wanted == "sandbox":
        raise Refused(f"the workspace cannot publish: {failure}; allocate git credentials on the host "
                      "(see the remote section of the dev README) or dispatch with --publish laptop")
    return {"mode": "laptop", "reason": failure}


def render_contract(mode: str, values: dict[str, str]) -> str:
    """unattended.md with only the publication block for `mode` and every placeholder filled."""
    kept, keep = [], True
    for line in CONTRACT.read_text(encoding="utf-8").splitlines(keepends=True):
        marker = re.match(r"^<!-- publish: (\w+) -->$", line.strip())
        if marker:
            keep = marker.group(1) in (mode, "end")
            continue
        if keep:
            kept.append(line)
    text = "".join(kept)
    for key, value in values.items():
        text = text.replace(key, value)
    return text


# --- commands ----------------------------------------------------------------

def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = load_config()
    host = Host(cfg)
    report = {"host": cfg.get("host", "local"), "checks": [], "workspaces": {}}

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"check": name, "ok": ok, "detail": detail})

    tools = host.text("for t in fleet incus git; do command -v $t >/dev/null && echo $t; done", check=False)
    check("host tools (fleet, incus, git)", tools.split() == ["fleet", "incus", "git"], tools.replace("\n", " "))
    src = cfg["sources_root"]
    check("sources root writable", host.sh(f"test -w {q(src)}", check=False).returncode == 0, src)
    try:
        states = host.instances()
    except (Refused, ValueError) as exc:
        states = {}
        check("incus list", False, str(exc))
    allowed = set(cfg["workspaces"]) or set(states)
    for ws in sorted(states):
        if ws not in allowed:
            continue
        entry = {"status": states[ws]}
        if states[ws] == "Running":
            model = ("python3 -c 'import json; print(json.load(open(\"/etc/claude-code/managed-settings.json\"))"
                     ".get(\"env\", {}).get(\"ANTHROPIC_MODEL\", \"\"))' 2>/dev/null")
            gh = ("if ! command -v gh >/dev/null; then echo missing; "
                  "elif timeout 30 gh auth status >/dev/null 2>&1; then echo signed-in; else echo signed-out; fi")
            info = host.vm_text(ws, f'echo "claude=$(claude --version 2>/dev/null | head -1)"; echo "model=$({model})"; '
                                    f'echo "lock=$(cat "{LOCK}/run" 2>/dev/null)"; echo "gh=$({gh})"', check=False)
            fields = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
            entry["claude"] = fields.get("claude") or "missing"
            entry["model"] = fields.get("model") or "host default"
            entry["lock"] = fields.get("lock") or None
            # Push access needs a repository, so dispatch probes it; this only says whether a PR could be opened.
            entry["gh"] = fields.get("gh") or "missing"
        report["workspaces"][ws] = entry
    check("a running workspace", any(w["status"] == "Running" for w in report["workspaces"].values()))
    check("claude in every running workspace",
          all(w.get("claude", "missing") != "missing" for w in report["workspaces"].values() if w["status"] == "Running"))
    print(json.dumps(report, indent=2))
    return 0 if all(c["ok"] for c in report["checks"]) else 1


def parse_phases(text: str) -> list[str]:
    phases = [p.strip() for p in text.split(",") if p.strip()]
    bad = [p for p in phases if p not in PHASES]
    if not phases or bad:
        raise Refused(f"phases must be a comma list of {', '.join(PHASES)} (got {text!r})")
    return phases


def committed_source(root: Path, branch: str) -> tuple[str, list[str]]:
    """The ref to send for `branch` (it or HEAD), refusing uncommitted tracked work; plus untracked files."""
    # An existing work branch is what the sandbox continues (a quick-mode ship round, a re-run);
    # otherwise the branch starts at HEAD. Either way it must be fully committed where it is checked out.
    exists = git_ok("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=root)
    source = f"refs/heads/{branch}" if exists else "HEAD"
    checkout = checked_out_at(root, branch) if exists else root
    if checkout is None:
        return source, []
    dirty = git("status", "--porcelain", "--untracked-files=no", cwd=checkout)
    if dirty:
        raise Refused(f"tracked files in {checkout} have uncommitted changes; the sandbox receives committed "
                      "work only:\n" + dirty)
    return source, [line[3:] for line in git("status", "--porcelain", cwd=checkout).splitlines()
                    if line.startswith("??")]


def merge_base(root: Path, source: str, default: str) -> str:
    # Ship diffs against this merge base, so a branch with no origin still measures from the local default.
    base_ref = next((ref for ref in (f"refs/remotes/origin/{default}", f"refs/heads/{default}")
                     if git_ok("rev-parse", "--verify", "-q", ref, cwd=root)), None)
    return git("merge-base", source, base_ref, cwd=root) if base_ref else git("rev-parse", source, cwd=root)


def refuse_busy_branch(root: Path, branch: str, run: str | None = None) -> None:
    for other in all_records():
        if (other.get("repo_root") == str(root) and other.get("branch") == branch
                and other.get("state") == "dispatched" and other["id"] != run):
            raise Refused(f"run {other['id']} is already working on {branch}; collect or release it first")


def deliver(host: Host, cfg: dict, rec: dict, root: Path, source: str) -> None:
    """Put `source` at refs/remotes/origin/{branch} in the workspace's checkout, through the exchange clone."""
    ws, repo, branch, origin_url = rec["workspace"], rec["repo"], rec["branch"], rec["origin_url"]
    exchange = f"{cfg['sources_root']}/{repo}"
    host_origin = host.text(f"if [ ! -d {q(exchange)}/.git ]; then git init -q {q(exchange)}; fi; "
                            f"git -C {q(exchange)} remote get-url origin 2>/dev/null || true")
    if host_origin and origin_url and normalize_url(host_origin) != normalize_url(origin_url):
        raise Refused(f"{exchange} on the host tracks {host_origin}, not {origin_url}; "
                      "rename this checkout or move the host clone")
    # The exchange clone is a private transport ref, never GitHub: force keeps it equal to this checkout.
    git("push", "-q", "--no-verify", "--force", host.git_url(exchange), f"{source}:refs/heads/{branch}", cwd=root)
    host.sh(f"fleet sync {q(ws)} {q(repo)}")
    # fleet sync reports success even when its fetch into an existing checkout was refused,
    # so the delivery is checked where the runner will look for it.
    sent = git("rev-parse", source, cwd=root)
    arrived = host.vm_text(ws, f"git -C \"$HOME/work/\"{q(repo)} rev-parse -q --verify "
                               f"{q(f'refs/remotes/origin/{branch}')} 2>/dev/null", check=False)
    if arrived != sent:
        raise Refused(f"fleet sync did not deliver {branch} to {ws} (the workspace has {arrived or 'no such ref'}, "
                      f"expected {sent[:12]}); a sync into an existing checkout fails silently when the host's "
                      "fleet fetches as root into the agent-owned clone (git: dubious ownership) - fix fleet "
                      f"sync on the host, or remove ~/work/{repo} in ws-{ws} so the next sync clones it fresh")


def run_files(stage: tarfile.TarFile, cfg: dict, rec: dict, round_: dict) -> None:
    """What every round refreshes in the run directory: the plugin, runner, contract, and run settings."""
    payload = {**{k: rec[k] for k in ("id", "repo", "plan", "branch", "base", "default_branch",
                                      "origin_url", "phases")},
               "publish": rec["publish"]["mode"], "github_repo": github_repo(rec["origin_url"]),
               "model": cfg["model"], "phase_timeout_seconds": int(float(cfg["phase_timeout_hours"]) * 3600),
               "view": cfg["view"]}
    contract = render_contract(rec["publish"]["mode"], {
        "{plugin}": f"{run_dir(rec)}/dev", "{plan}": rec["plan"], "{branch}": rec["branch"],
        "{default}": rec["default_branch"]})
    plugin_tar(stage, "dev")
    add_bytes(stage, "run.json", json.dumps(payload, indent=2).encode())
    add_bytes(stage, "round.json", json.dumps(round_, indent=2).encode())
    add_bytes(stage, "contract.md", contract.encode())
    add_bytes(stage, "runner.sh", RUNNER.read_bytes(), 0o755)


def plan_files(stage: tarfile.TarFile, rec: dict, plan_dir: Path) -> None:
    stage.add(plan_dir, arcname="plan")
    # Reports and evidence collected from an earlier run follow the plan, whichever sandbox runs next.
    for name in ("reports", "pr-evidence"):
        if (tmp_root() / rec["repo"] / name).is_dir():
            stage.add(tmp_root() / rec["repo"] / name, arcname=f"tmp/{name}")


def start_unit(host: Host, rec: dict, round_no: int) -> None:
    ws, run = q(rec["workspace"]), q(rec["id"])
    runner(host, rec, "queued", str(round_no))
    host.sh(f"incus exec ws-{ws} -- systemctl reset-failed dev-run-{run} 2>/dev/null; "
            f"incus exec ws-{ws} -- systemd-run --quiet --collect --unit=dev-run-{run} --uid=agent "
            f"/bin/bash -lc {q(q(run_dir(rec) + '/runner.sh') + ' run')}")


def cmd_dispatch(args: argparse.Namespace) -> int:
    cfg = load_config()
    root = Path(git("rev-parse", "--show-toplevel"))
    repo = root.name
    if not SAFE_NAME.match(repo):
        raise Refused(f"repository directory name {repo!r} is not a safe exchange name")
    phases = parse_phases(args.phases)
    if not SAFE_NAME.match(args.plan):
        raise Refused(f"invalid plan name {args.plan!r}")
    plan_dir = root / ".dev" / args.plan
    spec = plan_dir / "spec.md"
    if not plan_dir.is_dir():
        raise Refused(f"no plan directory {plan_dir}; run scope first")
    if phases[0] == "build":
        if not spec.exists():
            raise Refused(f"{spec} is missing; build needs a settled spec (run scope)")
        lint = subprocess.run([sys.executable, str(LINT_SPEC), str(spec)], capture_output=True, text=True, check=False)
        if lint.returncode != 0:
            raise Refused(f"the spec is not settled; lint-spec.py reports:\n{lint.stdout.strip()}")
    tracked = git("ls-files", f".dev/{args.plan}", cwd=root, check=False)
    if tracked or not git_ok("check-ignore", "--no-index", "-q", f".dev/{args.plan}/spec.md", cwd=root):
        # The plan reaches the sandbox inside the run's tarball and comes home through collect's merge;
        # a tracked copy would also travel through git, and the sandbox's `git add -A` commits it into the PR.
        fix = (f"untrack it with `git rm -r --cached .dev/{args.plan}` and commit that (the files stay on disk)"
               if tracked else "add `/.dev/*` and `!/.dev/config.yaml` to .gitignore and commit that")
        raise Refused(f".dev/{args.plan} is {'committed' if tracked else 'not git-ignored'}; plan files never go "
                      "through git: dispatch sends the plan directory to the sandbox with the run and collect brings "
                      f"it home, so it must stay out of commits - {fix} (plan-layout.md's ignore policy)")
    current = git("symbolic-ref", "-q", "--short", "HEAD", cwd=root, check=False)
    if not current:
        raise Refused("HEAD is detached; check out the branch to dispatch from")
    default = default_branch(root)
    branch = args.branch or (f"dev/{args.plan}" if current == default else current)
    if not git_ok("check-ref-format", "--branch", branch, cwd=root) or branch == default:
        raise Refused(f"cannot run on branch {branch!r}; pass --branch with a work branch name")
    source, untracked = committed_source(root, branch)
    base = merge_base(root, source, default)
    origin_url = git("remote", "get-url", "origin", cwd=root, check=False)
    refuse_busy_branch(root, branch)

    host = Host(cfg)
    stamp = datetime.now(timezone.utc).strftime("%y%m%d-%H%M")
    prefix = re.sub(r"[^a-z0-9-]", "-", args.plan.lower())[:32].strip("-")
    run = f"{prefix}-{stamp}-{os.urandom(2).hex()}"
    while record_path(run).exists():
        run = f"{prefix}-{stamp}-{os.urandom(2).hex()}"
    ws = acquire(host, cfg, args.workspace, run)
    rec = {"id": run, "repo": repo, "repo_root": str(root), "plan": args.plan, "branch": branch, "base": base,
           "default_branch": default, "origin_url": origin_url, "phases": phases, "workspace": ws,
           "host": cfg.get("host", "local"), "dispatched_at": now(), "manifest": manifest(plan_dir),
           "publish_requested": args.publish or cfg["publish"], "state": "dispatched"}
    try:
        rec["vm_home"] = host.vm_text(ws, 'printf %s "$HOME"')
        deliver(host, cfg, rec, root, source)
        rec["publish"] = resolve_publish(rec["publish_requested"], phases,
                                         lambda: probe_publish(host, ws, repo, branch, origin_url, run))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as stage:
            run_files(stage, cfg, rec, {"round": 1, "checkout": "fresh", "first": None})
            plan_files(stage, rec, plan_dir)
        d = run_dir(rec)
        host.vm(ws, f"rm -rf {q(d)} && mkdir -p {q(d)} && tar -xzf - -C {q(d)}", data=buffer.getvalue())
        start_unit(host, rec, 1)
    except Exception:
        release(host, rec)
        raise
    save_record(rec)
    view = host.text(f"fleet view {q(ws)}", check=False)
    print(json.dumps({"run": run, "workspace": ws, "branch": branch, "sent": source, "base": base,
                      "phases": phases, "publish": rec["publish"], "view": view or None,
                      "untracked_not_sent": untracked}, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config()
    host = Host(cfg)
    recs = [load_record(args.run)] if args.run else [
        r for r in all_records() if args.all or r.get("state") == "dispatched"]
    rows = []
    for rec in recs:
        live = run_state(host, rec) if rec.get("state") == "dispatched" else {"state": rec.get("state")}
        rows.append({"run": rec["id"], "workspace": rec["workspace"], "plan": rec["plan"], "branch": rec["branch"],
                     "state": live.get("state"), "phase": live.get("phase"), "reason": live.get("reason"),
                     "last_event_age": live.get("last_event_age"), "dispatched_at": rec["dispatched_at"]})
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("no active runs (use --all for collected ones)")
        return 0
    print(f"{'RUN':<40} {'WS':<6} {'STATE':<10} {'PHASE':<11} {'IDLE':<8} BRANCH")
    for r in rows:
        age = r["last_event_age"]
        idle = "-" if age is None else f"{int(age // 60)}m"
        print(f"{r['run']:<40} {r['workspace']:<6} {r['state'] or '?':<10} {r['phase'] or '-':<11} {idle:<8} "
              f"{r['branch']}{'  (' + r['reason'] + ')' if r.get('reason') else ''}")
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    rec = load_record(args.run)
    host = Host(load_config())
    offset = None
    while True:
        # Most raw lines are tool results that render to nothing, so --tail counts rendered lines.
        raw_tail = args.tail if args.raw else args.tail * 3
        out = runner(host, rec, "log", str(offset or 0), str(raw_tail), check=False)
        lines = out.stdout.decode(errors="replace").splitlines()
        header, body = (lines[0], lines[1:]) if lines and lines[0].startswith("#") else ("", lines)
        shown = body if args.raw else [text for line in body for text in render_event(line)]
        for text in shown if offset else shown[-args.tail:]:
            print(text)
        if header:
            offset = int(header.split()[-1])
        if not args.follow:
            return 0
        if run_state(host, rec).get("state") in TERMINAL:
            print(f"-- run {run_state(host, rec).get('state')}")
            return 0
        sys.stdout.flush()
        time.sleep(5)


def cmd_wait(args: argparse.Namespace) -> int:
    rec = load_record(args.run)
    host = Host(load_config())
    seen, failures = None, 0
    while True:
        try:
            live = run_state(host, rec)
            failures = 0
        except Refused as exc:
            failures += 1
            if failures >= args.max_failures:
                raise Refused(f"gave up after {failures} failed status checks: {exc}") from exc
            time.sleep(args.interval)
            continue
        marker = (live.get("state"), live.get("phase"))
        if marker != seen:
            print(f"{now()} {rec['id']}: {live.get('state')} {live.get('phase') or ''}".rstrip(), flush=True)
            seen = marker
        if live.get("state") in TERMINAL:
            return 0 if live.get("state") == "done" else 1
        time.sleep(args.interval)


def cmd_stop(args: argparse.Namespace) -> int:
    rec = load_record(args.run)
    host = Host(load_config())
    host.sh(f"incus exec ws-{q(rec['workspace'])} -- systemctl stop dev-run-{q(rec['id'])}", check=False)
    print(json.dumps({"run": rec["id"], **run_state(host, rec)}, indent=2))
    return 0


def collected_status(rec: dict) -> dict:
    try:
        status = json.loads((state_root() / "runs" / rec["id"] / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def resume_point(status: dict, phases: list[str], start: str | None, cont: str | None) -> dict:
    """Where a round starts: a phase's recorded session continued, or a phase started fresh.

    Without a choice, the phase that ended continues: the one that failed or stopped, or for
    a finished run the last phase that has a session.
    """
    sessions = status.get("session") if isinstance(status.get("session"), dict) else {}
    if start:
        phase, mode = start, "fresh"
    else:
        phase = cont or status.get("phase") or next((p for p in reversed(phases) if sessions.get(p)), phases[0])
        mode = "continue" if sessions.get(phase) or cont else "fresh"
    if phase not in phases:
        raise Refused(f"{phase} is not among the run's phases ({','.join(phases)}); pass --phases to include it")
    if mode == "continue" and not sessions.get(phase):
        raise Refused(f"no session was recorded for {phase}, so there is nothing to continue; "
                      f"--from {phase} starts it fresh")
    return {"phase": phase, "mode": mode, "session": sessions.get(phase, "") if mode == "continue" else "",
            "prompt": ""}


def project_dir(rec: dict) -> str:
    """Where Claude Code keeps transcripts of sessions run in the workspace checkout."""
    return f"{rec['vm_home']}/.claude/projects/" + re.sub(r"[^A-Za-z0-9]", "-", f"{rec['vm_home']}/work/{rec['repo']}")


def cmd_resume(args: argparse.Namespace) -> int:
    """Start a new round of a run that is not running, collected or not.

    In place while the run is uncollected: its workspace, checkout and plan files as they are.
    Restored once collected or released: this machine's branch and plan files are the truth,
    sent to the run's workspace when it is free (any free one otherwise), with the transcript
    of a session to continue when that workspace does not have it.
    """
    rec = load_record(args.run)
    cfg = load_config()
    host = Host(cfg)
    phases = parse_phases(args.phases) if args.phases else list(rec["phases"])
    in_place = rec.get("state") == "dispatched"
    if in_place:
        status = run_state(host, rec)
        if status.get("state") in ("starting", "running"):
            raise Refused(f"run {rec['id']} is {status['state']}; stop it first (remote.py stop {rec['id']})")
        if status.get("state") == "unknown":
            raise Refused(f"ws-{rec['workspace']} no longer holds run {rec['id']} ({status.get('detail') or 'no state'}); "
                          f"`remote.py release {rec['id']}`, then resume restores it from this machine")
    else:
        status = collected_status(rec)
    point = resume_point(status, phases, args.start, args.cont)
    point["prompt"] = args.prompt or ""
    remaining = phases[phases.index(point["phase"]):]
    round_no = len(rec.get("rounds", [])) + 2
    requested = args.publish or rec.get("publish_requested") or cfg["publish"]
    record = {**rec, "phases": phases}

    if in_place:
        record["publish"] = resolve_publish(requested, remaining, lambda: probe_publish(
            host, rec["workspace"], rec["repo"], rec["branch"], rec["origin_url"], rec["id"]))
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w:gz") as stage:
            run_files(stage, cfg, record, {"round": round_no, "checkout": "as-is", "first": point})
        d = run_dir(rec)
        host.vm(rec["workspace"], f"rm -rf {q(d)}/dev {q(d)}/sessions-in && tar -xzf - -C {q(d)}",
                data=buffer.getvalue())
        start_unit(host, record, round_no)
    else:
        root = Path(rec["repo_root"])
        if not (root / ".git").exists():
            raise Refused(f"{root} is not a git checkout any more; the run restores from it")
        branch = rec["branch"]
        if not git_ok("rev-parse", "--verify", "-q", f"refs/heads/{branch}", cwd=root):
            raise Refused(f"{branch} does not exist in {root}; nothing to restore the run from "
                          f"(commits a collect refused wait at refs/dev-remote/{rec['id']})")
        plan_dir = root / ".dev" / rec["plan"]
        if not plan_dir.is_dir():
            raise Refused(f"no plan directory {plan_dir} to restore the run with")
        source, _ = committed_source(root, branch)
        refuse_busy_branch(root, branch, rec["id"])
        artifacts = state_root() / "runs" / rec["id"]
        ws = acquire(host, cfg, args.workspace, rec["id"], prefer=rec["workspace"])
        record.update(workspace=ws, base=merge_base(root, source, rec["default_branch"]))
        try:
            record["vm_home"] = host.vm_text(ws, 'printf %s "$HOME"')
            deliver(host, cfg, record, root, source)
            record["publish"] = resolve_publish(requested, remaining, lambda: probe_publish(
                host, ws, rec["repo"], branch, rec["origin_url"], rec["id"]))
            d = run_dir(record)
            check = f"test -d {q(d)} && echo run; "
            if point["session"]:
                check += f"test -f {q(project_dir(record) + '/' + point['session'] + '.jsonl')} && echo session; "
            have = host.vm_text(ws, check + "true", check=False).split()
            send_session = bool(point["session"]) and "session" not in have
            if send_session and not (artifacts / "sessions" / f"{point['session']}.jsonl").exists():
                raise Refused(f"the transcript of {point['phase']}'s session {point['session']} is neither on "
                              f"ws-{ws} nor collected here; --from {point['phase']} starts it fresh")
            buffer = io.BytesIO()
            with tarfile.open(fileobj=buffer, mode="w:gz") as stage:
                run_files(stage, cfg, record, {"round": round_no,
                                               "checkout": "sync" if point["mode"] == "continue" else "fresh",
                                               "first": point})
                plan_files(stage, record, plan_dir)
                if send_session:
                    for name in (f"{point['session']}.jsonl", point["session"]):
                        if (artifacts / "sessions" / name).exists():
                            stage.add(artifacts / "sessions" / name, arcname=f"sessions-in/{name}")
                if "run" not in have:
                    # Another workspace: the run's history goes with it, so its logs and state continue.
                    for name in ("logs", "status.json", "sessions.txt"):
                        if (artifacts / name).exists():
                            stage.add(artifacts / name, arcname=name)
            host.vm(ws, f"mkdir -p {q(d)} && rm -rf {q(d)}/dev {q(d)}/plan {q(d)}/tmp {q(d)}/sessions-in "
                        f"&& tar -xzf - -C {q(d)}", data=buffer.getvalue())
            start_unit(host, record, round_no)
        except Exception:
            release(host, record)
            raise
        record.update(state="dispatched", manifest=manifest(plan_dir))
        for key in ("collected_at", "released_at", "outcome"):
            record.pop(key, None)

    record.setdefault("rounds", []).append({
        "round": round_no, "at": now(), "how": "in place" if in_place else "restored",
        "workspace": record["workspace"], "phases": phases, **point})
    save_record(record)
    view = host.text(f"fleet view {q(record['workspace'])}", check=False)
    print(json.dumps({"run": rec["id"], "round": round_no, "how": "in place" if in_place else "restored",
                      "workspace": record["workspace"], "phase": point["phase"], "mode": point["mode"],
                      "session": point["session"] or None, "phases": remaining, "publish": record["publish"],
                      "view": view or None}, indent=2))
    return 0


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


def cmd_collect(args: argparse.Namespace) -> int:
    rec = load_record(args.run)
    if rec.get("state") != "dispatched":
        raise Refused(f"run {rec['id']} was already {rec.get('state')}")
    cfg = load_config()
    host = Host(cfg)
    live = run_state(host, rec)
    if live.get("state") not in TERMINAL:
        raise Refused(f"run {rec['id']} is {live.get('state')}; wait for it or stop it before collecting")
    root = Path(rec["repo_root"])
    branch, ws = rec["branch"], rec["workspace"]
    summary: dict = {"run": rec["id"], "state": live.get("state"), "phase": live.get("phase"),
                     "reason": live.get("reason"), "workspace": ws, "branch": branch,
                     "publish": rec.get("publish", {"mode": "laptop"})}

    # 1. Commits: workspace -> exchange clone -> refs/dev-remote/{run} here, then fast-forward only.
    host.sh(f"fleet fetch {q(ws)} {q(rec['repo'])}")
    fetched_ref = f"refs/dev-remote/{rec['id']}"
    exchange = f"{cfg['sources_root']}/{rec['repo']}"
    got = git_ok("fetch", "-q", "--no-tags", host.git_url(exchange), f"+refs/remotes/ws-{ws}/{branch}:{fetched_ref}",
                 cwd=root)
    fetched = git("rev-parse", fetched_ref, cwd=root) if got else ""
    local_ref = f"refs/heads/{branch}"
    local = git("rev-parse", "--verify", "-q", local_ref, cwd=root, check=False)
    where = checked_out_at(root, branch)
    if not fetched:
        summary["branch_update"] = "none: the sandbox never had the branch checked out (see the logs)"
    elif not local:
        git("branch", branch, fetched, cwd=root)
        summary["branch_update"] = "created"
    elif local == fetched:
        summary["branch_update"] = "unchanged"
    elif git_ok("merge-base", "--is-ancestor", local, fetched, cwd=root):
        if where is not None:
            if git("status", "--porcelain", "--untracked-files=no", cwd=where):
                raise Refused(f"{branch} is checked out at {where} with uncommitted changes; commit or stash them "
                              f"and collect again (the sandbox's commits wait at {fetched_ref})")
            git("merge", "--ff-only", "-q", fetched, cwd=where)
        else:
            git("update-ref", local_ref, fetched, local, cwd=root)
        summary["branch_update"] = "fast-forwarded"
    elif git_ok("merge-base", "--is-ancestor", fetched, local, cwd=root):
        summary["branch_update"] = "local is ahead; left as is"
    else:
        raise Refused(f"{branch} moved here while the run worked and the histories diverged; the sandbox's commits "
                      f"wait at {fetched_ref} - reconcile by hand, then `remote.py release {rec['id']}`")
    summary["commits"] = (git("log", "--oneline", f"{rec['base']}..{branch}", cwd=root).splitlines()
                          if git_ok("rev-parse", "--verify", "-q", local_ref, cwd=root) else [])

    # 2. Everything else: one tar stream from the runner's pack command.
    packed = runner(host, rec, "pack", timeout=1800)
    with_dir = state_root() / "runs" / rec["id"]
    staged = with_dir / "staged"
    shutil.rmtree(staged, ignore_errors=True)
    extract_tar(packed.stdout, staged)
    plan_dir = root / ".dev" / rec["plan"]
    summary["plan"] = merge_plan(staged / "plan", plan_dir, rec.get("manifest", {})) if (staged / "plan").is_dir() \
        else {"updated": [], "conflicts": []}
    for name in ("reports", "pr-evidence"):
        if (staged / name).is_dir():
            shutil.copytree(staged / name, tmp_root() / rec["repo"] / name, dirs_exist_ok=True)
    if (staged / "run").is_dir():
        shutil.copytree(staged / "run", with_dir, dirs_exist_ok=True)
    if (staged / "sessions").is_dir():  # every phase session's transcript, so any later round can continue it
        shutil.copytree(staged / "sessions", with_dir / "sessions", dirs_exist_ok=True)
    summary["sessions"] = sorted(p.stem for p in (with_dir / "sessions").glob("*.jsonl"))
    summary["uncommitted_patches"] = sorted(str(p) for p in with_dir.glob("*.patch"))
    # The pull request the sandbox opened, as the runner read it back from the host after ship.
    try:
        summary["pr"] = json.loads((with_dir / "status.json").read_text(encoding="utf-8")).get("pr")
    except (OSError, ValueError):
        summary["pr"] = None

    # 3. A checkout of the branch for publication, without touching the user's own checkout.
    if where is None and not git_ok("rev-parse", "--verify", "-q", local_ref, cwd=root):
        where = root  # nothing came home to check out
    elif where is None:
        where = state_root() / "worktrees" / rec["id"] / rec["repo"]
        if not where.exists():
            where.parent.mkdir(parents=True, exist_ok=True)
            git("worktree", "add", "-q", str(where), branch, cwd=root)
    if where.resolve() != root.resolve():
        link = where / ".dev" / rec["plan"]
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.is_symlink() and not link.exists():
            link.symlink_to(plan_dir)
    summary["worktree"] = str(where)
    summary["plan_dir"] = str(plan_dir)
    summary["review"], summary["verdict"] = latest_review(plan_dir)
    summary["pr_md"] = str(plan_dir / "pr.md") if (plan_dir / "pr.md").exists() else None
    summary["open_calls"] = open_calls(plan_dir / "pr.md")
    summary["artifacts"] = str(with_dir)

    if not args.keep_lock:
        summary["workspace_released"] = release(host, rec)
    rec.update(state="collected", collected_at=now(), outcome=live.get("state"), worktree=str(where))
    save_record(rec)
    print(json.dumps(summary, indent=2))
    return 0


def cmd_release(args: argparse.Namespace) -> int:
    rec = load_record(args.run)
    host = Host(load_config())
    live = run_state(host, rec) if rec.get("state") == "dispatched" else {"state": rec.get("state")}
    if live.get("state") not in (*TERMINAL, "collected", "released", "unknown"):
        raise Refused(f"run {rec['id']} is {live.get('state')}; stop it first")
    released = release(host, rec)
    if rec.get("state") == "dispatched":
        rec.update(state="released", released_at=now())
        save_record(rec)
    print(json.dumps({"run": rec["id"], "workspace": rec["workspace"], "lock_released": released}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor")
    d = sub.add_parser("dispatch")
    d.add_argument("--plan", required=True)
    d.add_argument("--phases", default="build,ship")
    d.add_argument("--workspace")
    d.add_argument("--branch")
    d.add_argument("--publish", choices=PUBLISH, help="override the configured publish mode for this run")
    s = sub.add_parser("status")
    s.add_argument("run", nargs="?")
    s.add_argument("--all", action="store_true")
    s.add_argument("--json", action="store_true")
    lg = sub.add_parser("logs")
    lg.add_argument("run")
    lg.add_argument("--tail", type=int, default=40)
    lg.add_argument("--follow", action="store_true")
    lg.add_argument("--raw", action="store_true")
    w = sub.add_parser("wait")
    w.add_argument("run")
    w.add_argument("--interval", type=float, default=60)
    w.add_argument("--max-failures", type=int, default=30)
    for name in ("stop", "release"):
        sub.add_parser(name).add_argument("run")
    r = sub.add_parser("resume")
    r.add_argument("run")
    point = r.add_mutually_exclusive_group()
    point.add_argument("--continue", dest="cont", choices=PHASES, help="continue this phase's recorded session")
    point.add_argument("--from", dest="start", choices=PHASES, help="start this phase with a fresh session")
    r.add_argument("--prompt", help="an instruction added to the first session's prompt")
    r.add_argument("--phases", help="replace the run's phase list; the round runs from its phase to the end")
    r.add_argument("--workspace", help="restore a collected run on this workspace")
    r.add_argument("--publish", choices=PUBLISH, help="override the run's publish mode from this round on")
    c = sub.add_parser("collect")
    c.add_argument("run")
    c.add_argument("--keep-lock", action="store_true")
    args = parser.parse_args(argv)
    handler = {"doctor": cmd_doctor, "dispatch": cmd_dispatch, "status": cmd_status, "logs": cmd_logs,
               "wait": cmd_wait, "stop": cmd_stop, "resume": cmd_resume, "collect": cmd_collect,
               "release": cmd_release}[args.command]
    try:
        return handler(args)
    except Refused as exc:
        print(f"remote: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
