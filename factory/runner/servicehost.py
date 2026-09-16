"""Run an e2e driver with its services as one process group, so one guarded execution owns all of it.

    python3 -c <bootstrap> plan.json

The plan lists services ({id, argv, env, cwd, ready: {argv, env, cwd}, timeout_s}) and one
driver ({argv, env, cwd}). Services start in order and each must pass its readiness
command before the next starts; then the driver runs; then every service is stopped. The
exit status is the driver's, or 70 when a service never became ready. Service output goes to
`{log_dir}/{service}.log`. Cancellation and deadlines come from the guard around this
process, which stops the whole group.
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

NOT_READY = 70


def stop(processes: list[subprocess.Popen], grace: float = 5) -> None:
    for proc in reversed(processes):
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + grace
    for proc in reversed(processes):
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main(plan_path: str) -> int:
    plan = json.loads(Path(plan_path).read_text(encoding="utf-8"))
    log_dir = Path(plan["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    started: list[subprocess.Popen] = []
    try:
        for service in plan.get("services", []):
            log = open(log_dir / f"{service['id']}.log", "ab")
            proc = subprocess.Popen(service["argv"], cwd=service["cwd"], env=service["env"], stdin=subprocess.DEVNULL,
                                    stdout=log, stderr=subprocess.STDOUT)
            started.append(proc)
            ready = service["ready"]
            deadline = time.monotonic() + service.get("timeout_s", 60)
            while True:
                if proc.poll() is not None:
                    print(f"servicehost: service {service['id']} exited {proc.returncode} before it was ready",
                          file=sys.stderr, flush=True)
                    return NOT_READY
                check = subprocess.run(ready["argv"], cwd=ready["cwd"], env=ready["env"], stdin=subprocess.DEVNULL,
                                       capture_output=True)
                if check.returncode == 0:
                    print(f"servicehost: service {service['id']} ready", flush=True)
                    break
                if time.monotonic() > deadline:
                    print(f"servicehost: service {service['id']} not ready after {service.get('timeout_s', 60)}s",
                          file=sys.stderr, flush=True)
                    return NOT_READY
                time.sleep(0.2)
        driver = plan["driver"]
        return subprocess.run(driver["argv"], cwd=driver["cwd"], env=driver["env"], stdin=subprocess.DEVNULL).returncode
    finally:
        stop(started)


def bootstrap_argv(plan: Path) -> list[str]:
    from runner import FACTORY_ROOT
    code = (f"import sys; sys.path.insert(0, {str(FACTORY_ROOT)!r}); "
            "from runner.servicehost import main; sys.exit(main(sys.argv[1]))")
    return [sys.executable, "-c", code, str(plan)]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
