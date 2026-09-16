"""Guarded executions: every supervised command runs under a runner-owned guard process.

A guard holds the run's executor lease (`runs/{id}/executor.lock`) and the stage slot it
inherits from the worker, so neither looks free while the guard lives, even after the
worker dies. The child does not inherit them: a background process the agent leaves
behind must never wedge the run. The guard records the child's process identity before
a pipe barrier lets the child start work, enforces the shared deadline and cancellation,
and writes an execution receipt. When the guard itself is gone, liveness comes from the
recorded, verified child process group. The worker stays the only writer of run.json;
the guard writes only files in its execution directory and `executor.current`.

Execution directory files:
  request.json   written by the worker before launch
  executor.json  written by the guard: guard and child identity, state
  receipt.json   written by the guard at the end (or by recovery for a lost execution)
  guard.log      the guard's own diagnostics
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from runner import FACTORY_ROOT, faults
from runner.supervise import process_identity
from runner.model import atomic_write

REQUEST_SCHEMA = "factory.execution-request/1"
STATE_SCHEMA = "factory.executor/1"
RECEIPT_SCHEMA = "factory.receipt/1"
LEASE = "executor.lock"
CURRENT = "executor.current"
CLASSIFICATIONS = ("succeeded", "failed", "timeout", "cancelled", "budget", "spawn_failed", "refused", "lost",
                   "guard_error")

# The child side of the start barrier: wait for "go" before exec, report exec failure on a
# close-on-exec pipe, and never run the command when the guard vanished first.
SHIM = """
import os, sys
go_fd, status_fd = int(sys.argv[1]), int(sys.argv[2])
argv = sys.argv[3:]
data = b""
while True:
    chunk = os.read(go_fd, 16)
    if not chunk:
        break
    data += chunk
    if data.endswith(b"\\n"):
        break
os.close(go_fd)
if data != b"go\\n":
    os._exit(125)
os.set_inheritable(status_fd, False)
try:
    os.execvp(argv[0], argv)
except OSError as error:
    os.write(status_fd, f"{argv[0]}: {error}".encode("utf-8", "replace"))
    os._exit(127)
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ambiguous(Exception):
    """A process may belong to this run, but its identity cannot be verified; nothing was killed."""


@dataclass
class Request:
    token: str
    kind: str
    label: str
    argv: list[str]
    cwd: str
    run_dir: str
    stdout: str
    stderr: str
    deadline_at: float | None = None
    cancel_file: str | None = None
    grace: float = 30
    poll: float = 0.2
    inherit_fds: list[int] = field(default_factory=list)
    boundary: str = "host"
    token_ceiling: int | None = None
    schema: str = REQUEST_SCHEMA


@dataclass
class Receipt:
    token: str
    kind: str
    label: str
    classification: str
    exit_code: int | None
    started_at: str | None
    ended_at: str
    seconds: float
    spawned: bool
    spawn_error: str | None = None
    argv: list[str] = field(default_factory=list)
    argv_sha256: str | None = None
    cwd: str | None = None
    boundary: str = "host"
    deadline_at: float | None = None
    stdout: dict | None = None
    stderr: dict | None = None
    detail: str | None = None
    schema: str = RECEIPT_SCHEMA

    @property
    def cancelled(self) -> bool:
        return self.classification == "cancelled"

    @property
    def timed_out(self) -> bool:
        return self.classification == "timeout"

    def to_json(self) -> dict:
        return asdict(self)


def new_token() -> str:
    return uuid.uuid4().hex


# --- process identity ------------------------------------------------------------

def verified_alive(pid: int | None, start: str | None) -> bool | None:
    """True when pid is alive with this start identity, False when it is not ours or gone, None when unknowable."""
    exists, identity = process_identity(pid)
    if not exists:
        return False
    if start is None or identity is None:
        return None
    return identity == start


def group_alive(pgid: int | None) -> bool:
    if not pgid:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- the run-level lease -----------------------------------------------------------

def lease_held(run_dir: Path) -> bool:
    path = Path(run_dir) / LEASE
    if not path.exists():
        return False
    fd = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def current(run_dir: Path) -> dict | None:
    """The execution that last took the run's lease: {token, dir}."""
    return read_json(Path(run_dir) / CURRENT)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def read_state(exec_dir: Path) -> dict | None:
    return read_json(Path(exec_dir) / "executor.json")


def read_receipt(exec_dir: Path, token: str | None = None) -> Receipt | None:
    data = read_json(Path(exec_dir) / "receipt.json")
    if data is None or data.get("schema") != RECEIPT_SCHEMA:
        return None
    if token is not None and data.get("token") != token:
        return None
    known = set(Receipt.__dataclass_fields__)
    return Receipt(**{k: v for k, v in data.items() if k in known})


def file_digest(path: str | Path) -> dict:
    target = Path(path)
    if not target.is_file():
        return {"path": str(target), "sha256": None, "bytes": 0}
    digest = hashlib.sha256()
    size = 0
    with target.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return {"path": str(target), "sha256": digest.hexdigest(), "bytes": size}


# --- worker side ------------------------------------------------------------------------

def launch(exec_dir: Path, request: Request, env: dict) -> subprocess.Popen:
    """Write the request and start the guard in its own session. The guard inherits request.inherit_fds."""
    exec_dir = Path(exec_dir)
    exec_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("receipt.json", "executor.json"):
        (exec_dir / stale).unlink(missing_ok=True)
    atomic_write(exec_dir / "request.json", json.dumps(asdict(request), indent=2) + "\n")
    bootstrap = (f"import sys; sys.path.insert(0, {str(FACTORY_ROOT)!r}); "
                 "from runner.executor import guard_main; sys.exit(guard_main(sys.argv[1]))")
    with open(exec_dir / "guard.log", "ab") as log:
        return subprocess.Popen([sys.executable, "-c", bootstrap, str(exec_dir)], cwd="/", env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, pass_fds=tuple(request.inherit_fds))


def run(exec_dir: Path, request: Request, env: dict, *, on_running: Callable[[dict], None] = lambda state: None,
        cancel_requested: Callable[[], bool] = lambda: False) -> Receipt:
    """Launch a guarded execution and wait for its receipt."""
    guard = launch(exec_dir, request, env)
    reported = False
    while True:
        if not reported:
            state = read_state(exec_dir)
            if state and state.get("token") == request.token and state.get("child_pid"):
                on_running(state)
                reported = True
        if guard.poll() is not None:
            break
        time.sleep(min(request.poll, 0.1))
    receipt = read_receipt(exec_dir, request.token)
    if receipt is not None:
        if not reported:
            state = read_state(exec_dir)
            if state and state.get("child_pid"):
                on_running(state)
        return receipt
    return recover(Path(request.run_dir), exec_dir, request.token, cancel_requested=cancel_requested,
                   deadline_at=request.deadline_at, grace=request.grace, poll=request.poll)


def live(run_dir: Path) -> bool:
    """Whether an execution of this run may still be working. Raises Ambiguous when that cannot be decided."""
    if lease_held(run_dir):
        return True
    holder = current(run_dir)
    if not holder or not holder.get("dir"):
        return False
    state = read_state(Path(run_dir) / holder["dir"]) or {}
    return _child_live(state)


def _child_live(state: dict) -> bool:
    child = state.get("child_pid")
    if not child:
        return False
    ours = verified_alive(child, state.get("child_start"))
    if ours is None:
        raise Ambiguous(f"process {child} may still run this {state.get('kind', 'execution')}, but its identity "
                        "cannot be verified; stop it yourself if it is ours, then resume")
    if ours:
        return True
    # The leader is gone or its pid was reused. A surviving member of our group keeps pgid == child
    # only while no other process has taken that pid, and a process that took it is not a group member.
    exists, _ = process_identity(child)
    return not exists and group_alive(child)


def wait_idle(run_dir: Path, *, cancel_requested: Callable[[], bool], deadline_at: float | None, grace: float,
              poll: float = 0.2) -> None:
    """Block until no execution of the run is live, enforcing cancellation and the deadline once its guard is gone."""
    while live(run_dir):
        holder = current(run_dir) or {}
        exec_dir = Path(run_dir) / holder["dir"] if holder.get("dir") else None
        if exec_dir is not None and not lease_held(run_dir):
            finished = read_receipt(exec_dir, holder.get("token")) is not None
            expired = deadline_at is not None and time.time() >= deadline_at
            if finished or cancel_requested() or expired:
                _terminate_child(exec_dir, grace)
        time.sleep(poll)


def recover(run_dir: Path, exec_dir: Path, token: str, *, cancel_requested: Callable[[], bool] = lambda: False,
            deadline_at: float | None = None, grace: float = 30, poll: float = 0.2) -> Receipt:
    """The receipt of an execution whose worker (and maybe guard) went away.

    Waits while the execution is live, enforcing cancellation and the deadline when its
    guard is gone. A lost execution gets a synthesized `lost` receipt after its verified
    child is stopped. Raises Ambiguous rather than touching an unverifiable process.
    """
    exec_dir = Path(exec_dir)
    wait_idle(run_dir, cancel_requested=cancel_requested, deadline_at=deadline_at, grace=grace, poll=poll)
    receipt = read_receipt(exec_dir, token)
    if receipt is not None:
        return receipt
    state = read_state(exec_dir) or {}
    detail = "the guard ended without a receipt"
    if state.get("token") not in (None, token):
        detail = "the execution directory belongs to a different token"
    receipt = Receipt(token=token, kind=state.get("kind", "unknown"), label=state.get("label", ""),
                      classification="lost", exit_code=None, started_at=state.get("started_at"), ended_at=utc_now(),
                      seconds=0.0, spawned=bool(state.get("child_pid")), detail=detail)
    atomic_write(exec_dir / "receipt.json", json.dumps(receipt.to_json(), indent=2) + "\n")
    return receipt


def stop_live(run_dir: Path, grace: float, poll: float = 0.2) -> str:
    """Stop whatever execution of the run is live, by recorded and verified identity only.

    Returns "idle" when nothing was live and "stopped" otherwise; raises Ambiguous instead of guessing.
    """
    if not live(run_dir):
        return "idle"
    holder = current(run_dir) or {}
    exec_dir = Path(run_dir) / holder["dir"] if holder.get("dir") else None
    deadline = time.monotonic() + grace + 5
    signalled = False
    while live(run_dir) and time.monotonic() < deadline:
        if exec_dir is not None:
            state = read_state(exec_dir) or {}
            guard = verified_alive(state.get("guard_pid"), state.get("guard_start"))
            if guard and not signalled:
                os.kill(state["guard_pid"], signal.SIGTERM)
                signalled = True
            elif not guard:
                _terminate_child(exec_dir, grace)
        time.sleep(poll)
    if live(run_dir):
        raise Ambiguous(f"the execution recorded in {holder.get('dir')} did not stop within {grace + 5:.0f}s")
    return "stopped"


def _terminate_child(exec_dir: Path, grace: float) -> None:
    state = read_state(exec_dir) or {}
    if _child_live(state):
        _terminate_group(state["child_pid"], grace)


def _terminate_group(pgid: int, grace: float) -> None:
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline and group_alive(pgid):
        time.sleep(0.05)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


# --- guard side -----------------------------------------------------------------------------

class _Guard:
    def __init__(self, exec_dir: Path):
        self.exec_dir = exec_dir
        data = json.loads((exec_dir / "request.json").read_text(encoding="utf-8"))
        if data.get("schema") != REQUEST_SCHEMA:
            raise ValueError(f"unsupported execution request schema {data.get('schema')!r}")
        known = set(Request.__dataclass_fields__)
        self.request = Request(**{k: v for k, v in data.items() if k in known})
        self.stop = False
        self.started_at = utc_now()
        self.started = time.monotonic()
        self.state: dict = {}

    def log(self, message: str) -> None:
        print(f"{utc_now()} guard {os.getpid()}: {message}", flush=True)

    def write_state(self, **changes: object) -> None:
        self.state.update(changes)
        atomic_write(self.exec_dir / "executor.json", json.dumps(self.state, indent=2) + "\n")

    def cancelled(self) -> bool:
        return self.stop or bool(self.request.cancel_file and os.path.exists(self.request.cancel_file))

    def expired(self) -> bool:
        return self.request.deadline_at is not None and time.time() >= self.request.deadline_at

    def receipt(self, classification: str, *, exit_code: int | None = None, spawned: bool = False,
                spawn_error: str | None = None, detail: str | None = None) -> Receipt:
        request = self.request
        receipt = Receipt(
            token=request.token, kind=request.kind, label=request.label, classification=classification,
            exit_code=exit_code, started_at=self.started_at, ended_at=utc_now(),
            seconds=round(time.monotonic() - self.started, 3), spawned=spawned, spawn_error=spawn_error,
            argv=request.argv, argv_sha256=hashlib.sha256(json.dumps(request.argv).encode("utf-8")).hexdigest(),
            cwd=request.cwd, boundary=request.boundary, deadline_at=request.deadline_at,
            stdout=file_digest(request.stdout),
            stderr=file_digest(request.stderr), detail=detail,
        )
        atomic_write(self.exec_dir / "receipt.json", json.dumps(receipt.to_json(), indent=2) + "\n")
        self.write_state(state="exited", classification=classification)
        return receipt

    def run(self) -> int:
        request = self.request
        signal.signal(signal.SIGTERM, self._on_term)
        signal.signal(signal.SIGINT, self._on_term)
        lease_path = Path(request.run_dir) / LEASE
        lease = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = current(Path(request.run_dir)) or {}
            self.receipt("refused", detail=f"another execution ({holder.get('token')}) holds {lease_path}")
            return 3
        _, guard_start = process_identity(os.getpid())
        self.write_state(schema=STATE_SCHEMA, token=request.token, kind=request.kind, label=request.label,
                         guard_pid=os.getpid(), guard_start=guard_start, child_pid=None, child_start=None,
                         state="starting", started_at=self.started_at)
        atomic_write(Path(request.run_dir) / CURRENT,
                     json.dumps({"token": request.token, "dir": os.path.relpath(self.exec_dir, request.run_dir)}) + "\n")
        faults.point("guard.before_spawn", f"guard.before_spawn:{request.label}")
        if self.cancelled():
            self.receipt("cancelled", detail="cancellation was requested before launch")
            return 0
        if self.expired():
            self.receipt("timeout", detail="the attempt deadline had passed before launch")
            return 0
        go_read, go_write = os.pipe()
        status_read, status_write = os.pipe()
        inherit = [go_read, status_write]
        try:
            with open(request.stdout, "ab") as out, open(request.stderr, "ab") as err:
                child = subprocess.Popen(
                    [sys.executable, "-c", SHIM, str(go_read), str(status_write), *request.argv],
                    cwd=request.cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True,
                    pass_fds=tuple(inherit))
        except OSError as error:
            for fd in (go_read, go_write, status_read, status_write):
                os.close(fd)
            self.receipt("spawn_failed", spawn_error=f"{request.argv[0] if request.argv else '?'}: {error}")
            return 0
        os.close(go_read)
        os.close(status_write)
        _, child_start = process_identity(child.pid)
        self.write_state(child_pid=child.pid, child_start=child_start, pgid=child.pid, state="registered")
        faults.point("guard.before_go", f"guard.before_go:{request.label}")
        os.write(go_write, b"go\n")
        os.close(go_write)
        status = b""
        while True:
            chunk = os.read(status_read, 4096)
            if not chunk:
                break
            status += chunk
        os.close(status_read)
        if status:
            child.wait()
            self.receipt("spawn_failed", spawned=False, spawn_error=status.decode("utf-8", "replace"))
            return 0
        self.write_state(state="running")
        faults.point("guard.after_go", f"guard.after_go:{request.label}")
        classification = None
        usage = _UsageTail(request.stdout) if request.token_ceiling is not None else None
        detail = None
        while True:
            try:
                code = child.wait(timeout=request.poll)
                break
            except subprocess.TimeoutExpired:
                pass
            if usage is not None and usage.tokens() > request.token_ceiling:
                classification = "budget"
                detail = f"{usage.total} tokens reported against a remaining ceiling of {request.token_ceiling}"
                code = self._terminate(child)
                break
            if self.cancelled():
                classification = "cancelled"
                code = self._terminate(child)
                break
            if self.expired():
                classification = "timeout"
                code = self._terminate(child)
                break
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if classification is None:
            classification = "succeeded" if code == 0 else "failed"
        self.receipt(classification, exit_code=code, spawned=True, detail=detail)
        os.close(lease)
        return 0

    def _terminate(self, child: subprocess.Popen) -> int:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            return child.wait(timeout=self.request.grace)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return child.wait()

    def _on_term(self, signum: int, frame: object) -> None:
        self.stop = True


class _UsageTail:
    """Incremental reader of a Codex JSON event stream's reported usage (input plus output tokens)."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.offset = 0
        self.total = 0
        self.partial = b""

    def tokens(self) -> int:
        try:
            with self.path.open("rb") as handle:
                handle.seek(self.offset)
                chunk = handle.read()
        except OSError:
            return self.total
        self.offset += len(chunk)
        data = self.partial + chunk
        lines = data.split(b"\n")
        self.partial = lines.pop()
        for raw in lines:
            try:
                event = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
                self.total += int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
        return self.total


def guard_main(exec_dir: str) -> int:
    path = Path(exec_dir)
    try:
        guard = _Guard(path)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        print(f"{utc_now()} guard {os.getpid()}: unreadable request: {error}", flush=True)
        return 2
    try:
        return guard.run()
    except Exception as error:  # noqa: BLE001 - leave a receipt the worker can act on
        guard.log(f"crashed: {type(error).__name__}: {error}")
        child = guard.state.get("child_pid")
        if child:
            _terminate_group(child, guard.request.grace)
        try:
            guard.receipt("guard_error", spawned=bool(child), detail=f"{type(error).__name__}: {error}")
        except OSError:
            pass
        return 1
