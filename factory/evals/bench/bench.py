#!/usr/bin/env python3
"""Factory benchmark: run representative tasks, grade them independently, and report rates with their sample sizes.

    bench.py run --mode offline [--tasks id,id] [--variants name,name] [--repetitions N] --out DIR
    bench.py run --mode real --real-config FILE [--tasks id,id] [--repetitions N] --out DIR
    bench.py grade --run-dir RUN_DIR --task ID --out DIR     # grade a run started interactively with `factory new`
    bench.py report DIR [--baseline DIR] [--markdown FILE]

Offline mode drives stub hosts through scripted agent behaviors, so it measures the runner's
gates: a correct behavior must complete and pass the task's acceptance checks, and every
negative control (a broken implementation, inert tests, fabricated evidence, a missing review,
stale artifacts) must never reach `done`. Real mode runs the real hosts against a sandbox
GitHub repository under explicit model-call and run budgets.

Every run is headless: the harness seals the task's approved spec as the intent, exactly as a
scope handoff would, and records `"scope": "seeded"`. `grade` records `"scope": "interactive"`
for a run a person scoped. Acceptance checks live in this directory, never in the target
repository, and run against the revision the run produced.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

BENCH = Path(__file__).resolve().parent
FACTORY_ROOT = BENCH.parents[1]
sys.path.insert(0, str(FACTORY_ROOT))

from runner import control, events, intent, provenance, supervise  # noqa: E402
from runner import config as runner_config  # noqa: E402
from runner import worktree as wt  # noqa: E402
from runner.model import Run  # noqa: E402

STUBS = FACTORY_ROOT / "runner" / "tests" / "stubs"
TERMINAL = ("done", "needs-human", "cancelled")
RESULT_SCHEMA = "factory.bench-result/1"


def load_manifest() -> dict:
    return json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def copy_tree(source: Path, target: Path) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)


# --- the repository and the sealed intent -----------------------------------------------

def prepare_repo(root: Path, task: dict, remote_url: str | None = None, base_branch: str = "main") -> Path:
    """The task's starting repository: the shared app plus the task's base overlay, pushed to its origin."""
    data = BENCH / "tasks" / task["data"]
    repo = root / "repo"
    repo.mkdir(parents=True)
    copy_tree(BENCH / "apps" / task["app"], repo)
    if (data / "base").is_dir():
        copy_tree(data / "base", repo)
    git(repo, "init", "--quiet", "-b", base_branch)
    git(repo, "add", "-A")
    git(repo, "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", f"bench base for {task['id']}")
    if remote_url is None:
        origin = root / "origin.git"
        subprocess.run(["git", "init", "--quiet", "--bare", "-b", base_branch, str(origin)], check=True)
        remote_url = str(origin)
    git(repo, "remote", "add", "origin", remote_url)
    git(repo, "push", "--quiet", "-u", "origin", base_branch)
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{base_branch}")
    return repo


def seed_run(home: Path, repo: Path, task: dict, *, budget: int, base: str = "main") -> Run:
    """A run handed off with the task's spec sealed as its approved intent, queued for scope-review."""
    data = BENCH / "tasks" / task["data"]
    request = (data / "request.md").read_text(encoding="utf-8")
    run = Run.create(home / "runs", repo=str(repo), request=request.strip().splitlines()[0][:120], plan=task["id"],
                     body=request, source={"kind": "text"})
    created = wt.create(repo, run.worktree, task["id"], base=base)
    run.data.update({"base": created.base, "base_sha": created.base_sha, "branch": created.branch})
    run.plan_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(data / "spec.md", run.plan_dir / "spec.md")
    intent.snapshot_handoff(run)
    run.data.update({"status": "queued", "stage": "scope-review"})
    run.data["stages"]["scope"] = {"outcome": "done", "attempts": 1, "finished_at": run.data["created_at"]}
    run.data["retries"]["budget"] = budget
    run.data["bench"] = {"task": task["id"], "scope": "seeded"}
    run.save()
    run.event("run.queued", "scope-review", None, previous="scope")
    return run


# --- offline agent behaviors ---------------------------------------------------------------------

def at_head(path: str, record: dict) -> list[str]:
    script = ("import json, subprocess, sys; record = json.loads(sys.argv[2]); "
              "record['revision'] = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True)"
              ".stdout.strip(); open(sys.argv[1], 'w').write(json.dumps(record))")
    return ["python3", "-c", script, path, json.dumps(record)]


def review_record(lenses: list[str], kind: str) -> dict:
    return {"schema": "factory.review/1", "kind": kind, "revision": None, "diff_sha256": None,
            "expected_lenses": lenses, "completed_lenses": lenses, "missing_lenses": [], "invalid_results": {},
            "verification": {"required": [], "missing": [], "outcomes": {}}, "completeness": "complete",
            "verdict": "PASS", "blockers": [], "concerns": [], "refuted": [], "nits": [], "good": {}, "tasks": {}}


GAUNTLET_CHECKS = ("static-analysis", "security", "dead-code", "duplication", "dependency-rules",
                   "complexity-coverage", "flakiness", "mutation")

PR_BODY = """## Summary

{title}

## Evidence

**Before**

```json
{{"state": "base"}}
```

**After**

```json
{{"state": "change"}}
```
"""


def solution_files(data: Path) -> dict[str, str]:
    solution = data / "solution"
    files = {}
    for path in sorted(solution.rglob("*")) if solution.is_dir() else []:
        if path.is_file() and path.name != "scenario-map.json":
            files[path.relative_to(solution).as_posix()] = path.read_text(encoding="utf-8")
    return files


def offline_scenario(task: dict, variant: str) -> dict:
    """Stub host steps for one scripted behavior: the correct one, or a negative control."""
    data = BENCH / "tasks" / task["data"]
    app = BENCH / "apps" / task["app"]
    auto = "Question: how long are ids remembered - Answered: factory policy (auto-decided) - forever\n" \
        if task["kind"] == "ambiguous" else ""
    review_md = f"# Spec review 1 - {task['id']}\n\nVerdict: APPROVED\nRounds: 1\n\n## Escalations resolved\n{auto}\n"
    panel = review_record(["completeness", "consistency", "feasibility", "testability"], "spec")
    scope_review = {"files": {".dev/{plan}/spec-review_1.md": review_md,
                              ".dev/{plan}/spec-review_1.json": json.dumps(panel)}}
    if task["kind"] == "dependency":
        build = {"result": {"status": "blocked", "reason": "the payment sandbox rejects the run's requests",
                            "conditions": [{"code": "environment.missing_credentials",
                                            "summary": "PAYMENT_SANDBOX_KEY is not available to the run",
                                            "evidence": ["POST /refunds returned 401 without a key"],
                                            "requires_env": ["PAYMENT_SANDBOX_KEY"]}]}}
        return {"scope-review": [scope_review], "build": [build]}
    files = solution_files(data)
    mapping = (data / "solution" / "scenario-map.json").read_text(encoding="utf-8")
    notes = "## Change set 1\n- What was done: implemented the plan\n- Tests added: see scenario-map.json\n"
    if variant == "broken-impl":
        files["webhook.py"] = (app / "webhook.py").read_text(encoding="utf-8")
    elif variant == "inert-tests":
        files["webhook.py"] = (app / "webhook.py").read_text(encoding="utf-8")
        files["tests/test_webhook.py"] = ("import unittest\n\n\nclass WebhookTests(unittest.TestCase):\n"
                                          "    def test_repeated_id_ignored(self):\n        self.assertTrue(True)\n")
        files["e2e/run.py"] = ("import json, os\nrecord = {'schema': 'factory.e2e/1', 'plan': 'x', 'kind': 'non-frontend', "
                               "'revision': os.environ['FACTORY_REVISION'], 'scenarios': [{'id': 'duplicate-delivery', "
                               "'title': 'claimed', 'status': 'pass', 'states': [{'step': 's', 'entity': 'e', "
                               "'before': 1, 'after': 1}]}]}\njson.dump(record, open(os.path.join("
                               "os.environ['FACTORY_E2E_OUT'], 'e2e.json'), 'w'))\n")
    elif variant == "fabricated-evidence":
        files["webhook.py"] = (app / "webhook.py").read_text(encoding="utf-8")
        files["{report_dir}/{plan}-e2e.json"] = json.dumps({"schema": "factory.e2e/1", "plan": task["id"],
                                                            "kind": "non-frontend", "scenarios": [
                                                                {"id": "duplicate-delivery", "title": "claimed",
                                                                 "status": "pass"}]})
        files[".dev/{plan}/test-results.json"] = json.dumps({"schema": "factory.test-results/1", "tests": [
            {"id": "tests/test_webhook.py::WebhookTests::test_repeated_id_ignored", "outcome": "passed"}]})
    build = {"files": {**files, ".dev/{plan}/scenario-map.json": mapping,
                       ".dev/{plan}/implementation-notes.md": notes},
             "run": [["git", "add", "-A"], ["git", "commit", "--quiet", "-m", f"feat: {task['id']}"]]}
    if variant == "fabricated-evidence":
        build["result"] = {"status": "done", "reason": "all scenarios pass", "conditions": []}
    gauntlet = {"schema": "factory.gauntlet/1", "revision": None, "checks": [
        {"id": check, "applicable": check == "static-analysis",
         **({"command": {"run": ["python3", "-m", "py_compile", "webhook.py"]}, "tool_version": "py_compile",
             "scope": "webhook.py", "threshold_source": "default", "surviving": []}
            if check == "static-analysis" else {"reason": "contract or no docs/dependencies.md"})}
        for check in GAUNTLET_CHECKS]}
    create = ["{gh}", "pr", "create", "--title", task["id"], "--body-file", ".dev/{plan}/pr.md"]
    ship_run = [["git", "push", "--quiet", "origin", "HEAD"],
                ["sh", "-c", "{gh} pr list --head {branch} --json number | grep -q '\"number\"' || "
                 + " ".join(f"'{part}'" for part in create)]]
    records = [at_head(".dev/{plan}/review_1.json", review_record(["correctness", "simplify", "spec-conformance",
                                                                   "tests"], "code")),
               at_head(".dev/{plan}/gauntlet.json", gauntlet)]
    if variant == "missing-review":
        records = records[1:]
    ship_run += records
    if variant == "stale-artifacts":
        ship_run += [["sh", "-c", "echo '# notes' >> README.md && git commit --quiet -am 'docs: late note' && "
                                  "git push --quiet origin HEAD"]]
    ship = {"files": {".dev/{plan}/review_1.md": "# Review 1\n\nVerdict: PASS\n\n## Blockers\n",
                      ".dev/{plan}/pr.md": PR_BODY.format(title=task["id"])},
            "run": ship_run, "result": {"pr_url": "https://github.com/stub/repo/pull/1", "draft": False}}
    return {"scope-review": [scope_review], "build": [build], "ship": [ship]}


# --- running and grading ---------------------------------------------------------------------------

@contextmanager
def offline_environment(root: Path, scenario: dict):
    home = root / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"notify": False, "stage_poll_seconds": 0.05,
                                                  "heartbeat_seconds": 0.2}), encoding="utf-8")
    (root / "scenario.json").write_text(json.dumps(scenario), encoding="utf-8")
    (root / "gh-state.json").write_text(json.dumps({"auth": True, "prs": [], "checks": {}, "default_checks": [
        {"name": "ci", "state": "SUCCESS", "bucket": "pass"}]}), encoding="utf-8")
    (root / "gitconfig").write_text("[user]\n\tname = Factory Bench\n\temail = bench@factory.invalid\n"
                                    "[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n", encoding="utf-8")
    env = {"FACTORY_HOME": str(home), "FACTORY_CODEX_BIN": str(STUBS / "codex"), "FACTORY_CLAUDE_BIN": str(STUBS / "claude"),
           "FACTORY_GH_BIN": str(STUBS / "gh"), "FACTORY_STUB_SCENARIO": str(root / "scenario.json"),
           "FACTORY_STUB_LOG": str(root / "stub-log.jsonl"), "FACTORY_GH_STATE": str(root / "gh-state.json"),
           "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "CODEX_HOME": str(root / "codex-home")}
    with mock.patch.dict(os.environ, env):
        yield home


@contextmanager
def real_environment(root: Path, settings: dict):
    home = root / "home"
    home.mkdir()
    (home / "config.json").write_text(json.dumps({"notify": False, "max_tokens_per_run": settings["max_tokens_per_run"],
                                                  "max_concurrent_stages": 1}), encoding="utf-8")
    yield home


def wait_terminal(run_dir: Path, *, timeout: float, fault: str | None, home: Path) -> tuple[dict, int]:
    """Wait for a terminal status; resume a worker the harness's fault killed. Returns (run.json, resumes)."""
    resumes = 0
    deadline = time.monotonic() + timeout
    cfg = runner_config.load(home)
    while time.monotonic() < deadline:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        alive = supervise.worker_alive(run_dir)
        if data["status"] in TERMINAL and not alive:
            return data, resumes
        if not alive and data["status"] in ("running", "queued") and fault:
            time.sleep(0.2)
            if not supervise.worker_alive(run_dir):
                control.resume(home, cfg, run_dir)
                resumes += 1
        time.sleep(0.2)
    raise TimeoutError(f"{run_dir.name} did not finish within {timeout:.0f}s")


def grade(run_dir: Path, task: dict) -> dict:
    """Run the task's acceptance checks against the revision the run produced, outside its repository."""
    data = BENCH / "tasks" / task["data"]
    acceptance = data / "acceptance"
    worktree = run_dir / "worktree"
    if not acceptance.is_dir():
        return {"applicable": False}
    if not worktree.is_dir():
        return {"applicable": True, "passed": False, "output": "the run has no worktree to grade"}
    revision = git(worktree, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="bench-grade-") as tmp:
        archive = Path(tmp) / "revision.tar"
        with archive.open("wb") as handle:
            subprocess.run(["git", "-C", str(worktree), "archive", "--format=tar", revision], stdout=handle, check=True)
        checkout = Path(tmp) / "checkout"
        checkout.mkdir()
        with tarfile.open(archive) as bundle:
            bundle.extractall(checkout, filter="data") if sys.version_info >= (3, 12) else bundle.extractall(checkout)
        result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", str(acceptance), "-t",
                                 str(acceptance)], capture_output=True, text=True, timeout=600,
                                env={**os.environ, "BENCH_CHECKOUT": str(checkout), "PYTHONDONTWRITEBYTECODE": "1"})
    return {"applicable": True, "passed": result.returncode == 0, "revision": revision,
            "output": (result.stdout + result.stderr)[-4000:]}


def summarize(run_dir: Path, data: dict, task: dict, variant: str, role: str, mode: str, scope: str,
              repetition: int, grading: dict, resumes: int, fault: str | None, seconds: float) -> dict:
    log = events.read(run_dir)
    attempts = [{"stage": a["stage"], "n": a["n"], "outcome": a.get("outcome"), "code": a.get("code"),
                 "seconds": a.get("seconds"), "host_seconds": a.get("host_seconds"),
                 "gate_seconds": a.get("gate_seconds"), "tokens": a.get("tokens", 0)}
                for a in data["attempts"]]
    runtimes = []
    for attempt in data["attempts"]:
        path = run_dir / "attempts" / f"{attempt['stage']}-{attempt['n']}" / "runtime.json"
        if path.is_file():
            runtimes.append(json.loads(path.read_text(encoding="utf-8")))
    queued = [e for e in log if e["event"] in ("run.queued", "slot.acquired")]
    queue_seconds = 0.0
    waiting_since = None
    for entry in queued:
        stamp = time.mktime(time.strptime(entry["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        if entry["event"] == "run.queued":
            waiting_since = stamp
        elif waiting_since is not None:
            queue_seconds += stamp - waiting_since
            waiting_since = None
    expected = task["expected"]
    status = data["status"]
    plan_dir = run_dir / "worktree" / ".dev" / data["plan"]
    audit = "".join(path.read_text(encoding="utf-8", errors="replace") + "\n"
                    for pattern in ("spec-review_*.md", "implementation-notes.md", "review_*.md")
                    for path in sorted(plan_dir.glob(pattern)))
    auto_decided = len(intent.auto_decisions(audit))
    conditions = [c["code"] for c in data.get("conditions", [])]
    meets = status == expected["status"]
    if expected.get("condition"):
        meets = meets and expected["condition"] in conditions
    if expected.get("min_auto_decided"):
        meets = meets and auto_decided >= expected["min_auto_decided"]
    if grading.get("applicable") and expected["status"] == "done":
        meets = meets and grading.get("passed", False)
    return {
        "schema": RESULT_SCHEMA,
        "bench_version": load_manifest()["version"],
        "task": {"id": task["id"], "version": task["version"], "kind": task["kind"]},
        "variant": variant, "role": role, "mode": mode, "scope": scope, "repetition": repetition,
        "run_id": data["id"], "status": status, "stage": data["stage"], "reason": data["human"].get("reason"),
        "revision": grading.get("revision"), "acceptance": grading,
        "success": role == "positive" and meets,
        "false_green": status == "done" and grading.get("applicable", False) and not grading.get("passed", False),
        "rejected": status != "done",
        "interventions": sum(1 for e in log if e["event"] == "run.needs_human"),
        "fault": fault, "resumes": resumes, "recovered": bool(fault) and meets,
        "auto_decided": auto_decided, "conditions": conditions,
        "attempts": attempts, "retries": data["retries"],
        "timing": {"elapsed_seconds": round(seconds, 1), "queue_seconds": queue_seconds,
                   "host_seconds": round(sum(a.get("host_seconds") or 0 for a in attempts), 1),
                   "gate_seconds": round(sum(a.get("gate_seconds") or 0 for a in attempts), 1)},
        "tokens": data.get("tokens_total", 0),
        "runtime": {"runner": sorted({r["runner"]["content_sha256"] for r in runtimes}),
                    "skills": sorted({r["skills"]["skills_id"] for r in runtimes}),
                    "hosts": runtimes[-1]["hosts"] if runtimes else None,
                    "models": sorted({f"{r['model']}/{r['effort']}" for r in runtimes})},
    }


def retain(run_dir: Path, target: Path) -> None:
    """Keep what explains a result: run state, events, intent, and each attempt's small records."""
    target.mkdir(parents=True, exist_ok=True)
    for name in ("run.json", "events.jsonl"):
        if (run_dir / name).is_file():
            shutil.copyfile(run_dir / name, target / name)
    if (run_dir / "intent").is_dir():
        shutil.copytree(run_dir / "intent", target / "intent", dirs_exist_ok=True)
    for attempt in sorted((run_dir / "attempts").iterdir()) if (run_dir / "attempts").is_dir() else []:
        for name in ("gate.json", "runtime.json", "gate-executions.json", "prompt.txt"):
            if (attempt / name).is_file():
                (target / "attempts" / attempt.name).mkdir(parents=True, exist_ok=True)
                shutil.copyfile(attempt / name, target / "attempts" / attempt.name / name)


def run_one(task: dict, variant: str, role: str, *, mode: str, repetition: int, out: Path, budget: int,
            timeout: float, settings: dict | None = None) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"bench-{task['id']}-"))
    fault = task.get("fault")
    started = time.monotonic()
    try:
        if mode == "offline":
            context = offline_environment(root, offline_scenario(task, variant))
        else:
            context = real_environment(root, settings)
        with context as home:
            base = "main"
            remote = None
            if mode == "real":
                base = f"factory-bench/{uuid.uuid4().hex[:8]}/{task['id']}"
                remote = settings["remote_url"]
            repo = prepare_repo(root, task, remote_url=remote, base_branch=base)
            run = seed_run(home, repo, task, budget=budget, base=base)
            fault_env = {"FACTORY_FAULT": fault, "FACTORY_FAULT_ONCE": str(root / "fault-fired")} if fault else {}
            with mock.patch.dict(os.environ, fault_env):
                control.start_worker(home, run)
            data, resumes = wait_terminal(run.dir, timeout=timeout, fault=fault, home=home)
            grading = grade(run.dir, task)
            result = summarize(run.dir, data, task, variant, role, mode, "seeded", repetition, grading, resumes, fault,
                               time.monotonic() - started)
            target = out / task["id"] / f"{variant}-{repetition}"
            retain(run.dir, target / "run")
            (target / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
            return result
    finally:
        shutil.rmtree(root, ignore_errors=True)


def cmd_run(args: argparse.Namespace) -> int:
    manifest = load_manifest()
    wanted = set(args.tasks.split(",")) if args.tasks else None
    variants = set(args.variants.split(",")) if args.variants else None
    settings = None
    if args.mode == "real":
        if not args.real_config:
            print("bench: real mode needs --real-config with remote_url, max_tokens_per_run, and max_runs", file=sys.stderr)
            return 2
        settings = json.loads(Path(args.real_config).read_text(encoding="utf-8"))
        missing = [key for key in ("remote_url", "max_tokens_per_run", "max_runs") if key not in settings]
        if missing:
            print(f"bench: real config lacks {', '.join(missing)}", file=sys.stderr)
            return 2
    plan = [(task, variant, role, rep) for task in manifest["tasks"]
            if args.mode in task["modes"] and (wanted is None or task["id"] in wanted)
            for variant, role in task["variants"].items() if variants is None or variant in variants
            for rep in range(1, args.repetitions + 1)]
    if settings and len(plan) > settings["max_runs"]:
        print(f"bench: {len(plan)} planned runs exceed max_runs {settings['max_runs']}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps({"bench_version": manifest["version"], "mode": args.mode,
                                               "runner": provenance.runner_identity(),
                                               "runs": [f"{t['id']}/{v}-{r}" for t, v, _, r in plan]}, indent=2) + "\n")
    for task, variant, role, rep in plan:
        result = run_one(task, variant, role, mode=args.mode, repetition=rep, out=out, budget=args.budget,
                         timeout=args.timeout, settings=settings)
        print(f"{task['id']:<28} {variant:<20} rep {rep}: {result['status']:<12} "
              f"{'success' if result['success'] else 'rejected' if result['rejected'] else 'done'}"
              f"{' FALSE GREEN' if result['false_green'] else ''}", flush=True)
    return 0


def cmd_grade(args: argparse.Namespace) -> int:
    manifest = {task["id"]: task for task in load_manifest()["tasks"]}
    task = manifest[args.task]
    run_dir = Path(args.run_dir)
    data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    result = summarize(run_dir, data, task, "interactive", "positive", "real", "interactive", 1, grade(run_dir, task),
                       0, None, 0.0)
    target = Path(args.out) / task["id"] / f"interactive-{data['id']}"
    retain(run_dir, target / "run")
    (target / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("status", "success", "false_green")}))
    return 0


# --- reporting ---------------------------------------------------------------------------------------

def load_results(directory: Path) -> list[dict]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(Path(directory).rglob("result.json"))]


def rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "rate": round(numerator / denominator, 4) if denominator else None}


def metrics(results: list[dict]) -> dict:
    positive = [r for r in results if r["role"] == "positive"]
    negative = [r for r in results if r["role"] == "negative"]
    done = [r for r in results if r["status"] == "done"]
    faulted = [r for r in results if r["fault"]]
    per_task: dict[str, list[int]] = {}
    for result in positive:
        per_task.setdefault(result["task"]["id"], []).append(1 if result["success"] else 0)
    elapsed = [r["timing"]["elapsed_seconds"] for r in results]
    return {
        "runs": len(results),
        "repetitions": max((r["repetition"] for r in results), default=0),
        "task_success": rate(sum(r["success"] for r in positive), len(positive)),
        "false_green": rate(sum(r["false_green"] for r in done), len(done)),
        "negative_controls_rejected": rate(sum(r["rejected"] for r in negative), len(negative)),
        "intervention": rate(sum(1 for r in results if r["interventions"]), len(results)),
        "recovery_success": rate(sum(r["recovered"] for r in faulted), len(faulted)),
        "per_task": {task: {"success": rate(sum(values), len(values)),
                            "variance": round(statistics.pvariance(values), 4) if len(values) > 1 else 0.0}
                     for task, values in sorted(per_task.items())},
        "resources": {
            "elapsed_seconds_median": statistics.median(elapsed) if elapsed else None,
            "queue_seconds_total": round(sum(r["timing"]["queue_seconds"] for r in results), 1),
            "host_seconds_total": round(sum(r["timing"]["host_seconds"] for r in results), 1),
            "gate_seconds_total": round(sum(r["timing"]["gate_seconds"] for r in results), 1),
            "tokens_total": sum(r["tokens"] for r in results),
        },
        "versions": {"bench": sorted({r["bench_version"] for r in results}),
                     "runner": sorted({h for r in results for h in r["runtime"]["runner"]}),
                     "skills": sorted({s for r in results for s in r["runtime"]["skills"]}),
                     "modes": sorted({f"{r['mode']}/{r['scope']}" for r in results})},
    }


def render_rate(name: str, value: dict) -> str:
    shown = "n/a" if value["rate"] is None else f"{value['rate']:.1%}"
    return f"| {name} | {value['numerator']} / {value['denominator']} | {shown} |"


def cmd_report(args: argparse.Namespace) -> int:
    candidate = metrics(load_results(Path(args.results)))
    report = {"candidate": candidate}
    if args.baseline:
        report["baseline"] = metrics(load_results(Path(args.baseline)))
    print(json.dumps(report, indent=2))
    if args.markdown:
        lines = ["# Factory benchmark report", "",
                 f"Runs: {candidate['runs']}, repetitions per task and variant: {candidate['repetitions']}.",
                 f"Modes: {', '.join(candidate['versions']['modes'])}; bench {', '.join(candidate['versions']['bench'])}.",
                 f"Runner content: {', '.join(h[:12] for h in candidate['versions']['runner']) or 'unknown'}; "
                 f"skills: {', '.join(s[:19] for s in candidate['versions']['skills']) or 'unknown'}.", "",
                 "| Metric | Count | Rate |", "| --- | --- | --- |"]
        for key, label in (("task_success", "Task success"), ("false_green", "False green"),
                           ("negative_controls_rejected", "Negative controls rejected"),
                           ("intervention", "Runs needing intervention"), ("recovery_success", "Recovery success")):
            lines.append(render_rate(label, candidate[key]))
            if "baseline" in report:
                lines.append(render_rate(f"{label} (baseline)", report["baseline"][key]))
        lines += ["", "Small samples establish behavior on this corpus, not a reliability rate.", ""]
        Path(args.markdown).write_text("\n".join(lines), encoding="utf-8")
    failures = candidate["false_green"]["numerator"] + (candidate["negative_controls_rejected"]["denominator"]
                                                        - candidate["negative_controls_rejected"]["numerator"])
    if args.require_success:
        success = candidate["task_success"]
        failures += success["denominator"] - success["numerator"]
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=["offline", "real"], required=True)
    run.add_argument("--tasks")
    run.add_argument("--variants")
    run.add_argument("--repetitions", type=int, default=1)
    run.add_argument("--budget", type=int, default=1, help="retries per run (the product default is 5)")
    run.add_argument("--timeout", type=float, default=300)
    run.add_argument("--real-config")
    run.add_argument("--out", required=True)
    run.set_defaults(func=cmd_run)
    graded = sub.add_parser("grade")
    graded.add_argument("--run-dir", required=True)
    graded.add_argument("--task", required=True)
    graded.add_argument("--out", required=True)
    graded.set_defaults(func=cmd_grade)
    report = sub.add_parser("report")
    report.add_argument("results")
    report.add_argument("--baseline")
    report.add_argument("--markdown")
    report.add_argument("--require-success", action="store_true",
                        help="also fail unless every positive run succeeded (the offline release gate)")
    report.set_defaults(func=cmd_report)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
