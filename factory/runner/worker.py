"""The detached per-run worker: slot, attempt, gate, transition, repeat.

While alive, the worker holding runs/{id}/worker.lock is the only writer of run.json.
"""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from runner import (FACTORY_ROOT, browser, commands, conditions, config, dashboard, events, executor, faults, foreman,
                    gates, hosts, intent, notify, pricing, provenance, publication, records, slots, supervise)
from runner import worktree as wt
from runner.model import (CANCELLED, DONE, PARKED, QUEUED, RUNNING, BudgetExhausted, Run, decide, enforce,
                          remember_guidance, utc_now)
from runner.pipeline import PIPELINE, Stage, gate_context, gate_reserve_s, repair_prompt, write_run_context


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"{stamp} {message}", flush=True)


# Handles to detached workers, so a long-lived caller can reap them instead of leaking zombies.
_DETACHED: list[subprocess.Popen] = []


def spawn_worker(home: Path, run: Run) -> int:
    """Start the detached worker process for a run; returns its PID."""
    _DETACHED[:] = [proc for proc in _DETACHED if proc.poll() is None]
    env = dict(os.environ)
    env["FACTORY_HOME"] = str(home)
    bootstrap = (f"import sys; sys.path.insert(0, {str(FACTORY_ROOT)!r}); "
                 "from runner.worker import main; sys.exit(main(sys.argv[1]))")
    with open(run.dir / "worker.log", "ab") as worker_log:
        proc = subprocess.Popen(
            [sys.executable, "-c", bootstrap, run.id],
            cwd="/",
            stdin=subprocess.DEVNULL,
            stdout=worker_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=env,
        )
    _DETACHED.append(proc)
    return proc.pid


class Worker:
    def __init__(self, home: Path, run_id: str, *, pipeline: dict[str, Stage] | None = None,
                 grace: float = supervise.KILL_GRACE_SECONDS, sleep=time.sleep):
        self.home = home
        self.run_dir = home / "runs" / run_id
        self.pipeline = pipeline or PIPELINE
        self.grace = grace
        self.sleep = sleep
        self.cfg = config.load(home)

    # --- plumbing -----------------------------------------------------------------

    def reload_config(self) -> None:
        try:
            self.cfg = config.load(self.home)
        except config.ConfigError as error:
            log(f"config reload failed, keeping previous values: {error}")

    def refresh(self) -> None:
        dashboard.safe_regenerate(self.home, heartbeat_seconds=self.cfg.heartbeat_seconds, log=log)

    def save(self, run: Run) -> None:
        run.save()
        self.refresh()

    # --- main loop ------------------------------------------------------------------

    def run(self) -> int:
        lock = supervise.WorkerLock(self.run_dir)
        if not lock.acquire():
            log("another worker holds the lock; exiting")
            return 3
        try:
            supervise.write_pid(self.run_dir)
            with supervise.Heartbeat(self.run_dir, self.cfg.heartbeat_seconds):
                return self.loop()
        finally:
            lock.release()

    def loop(self) -> int:
        emit_pending(Run.load(self.run_dir))
        while True:
            self.reload_config()
            run = Run.load(self.run_dir)
            if run.status == RUNNING:
                undecided = run.undecided_attempt()
                if undecided is not None:
                    self.decide_next(run, undecided)
                else:
                    self.recover(run)
                continue
            if run.status in PARKED:
                log(f"run is {run.status}; worker exiting")
                return 0
            if run.cancel_requested:
                self.cancel(run, "cancelled by operator")
                return 0
            if run.status == QUEUED and run.pause_requested:
                self.pause(run)
                return 0
            if run.status != QUEUED:
                log(f"unexpected status {run.status}; worker exiting")
                return 2
            stage = self.pipeline[run.stage]
            if (run.data.get("wait") or {}).get("stage") == stage.name:
                if not self.wait(run, stage):
                    return 0
                continue
            if not stage.slot_limited:
                run.transition("slot_acquired")
                self.save(run)
                self.execute(run, stage)
                continue
            slot = self.acquire_slot(run, abort_on_pause=True)
            if slot is None:
                if run.cancel_requested:
                    self.cancel(Run.load(self.run_dir), "cancelled by operator while queued")
                else:
                    self.pause(Run.load(self.run_dir))
                return 0
            try:
                run = Run.load(self.run_dir)
                run.transition("slot_acquired")
                run.data["slot"] = slot.index
                self.save(run)
                run.event("slot.acquired", slot=slot.index)
                self.dispatch(run, stage, slot)
            finally:
                slot.release()
                released = Run.load(self.run_dir)
                released.data["slot"] = None
                released.save()
                released.event("slot.released", slot=slot.index)

    def wait(self, run: Run, stage: Stage) -> bool:
        """Hold the queued run, with no slot, until its probe passes or its time is up; False when the run stops."""
        spec = run.data["wait"]
        until = float(spec.get("until") or 0)
        observed = None
        while True:
            if run.cancel_requested:
                self.cancel(run, "cancelled by operator while waiting")
                return False
            if run.pause_requested:
                self.pause(run)
                return False
            passed, observed = probe(spec.get("probe") or {})
            if passed or time.time() >= until:
                break
            self.sleep(min(self.cfg.stage_poll_seconds, max(0.0, until - time.time())) or self.cfg.stage_poll_seconds)
            self.reload_config()
            run = Run.load(self.run_dir)
            run.data["wait"] = spec
        elapsed = max(0.0, time.time() - float(spec.get("started") or until))
        run.data.pop("wait", None)
        waits = run.data.setdefault("waits", {})
        waits[stage.name] = round(float(waits.get(stage.name, 0)) + elapsed, 1)
        run.data["last_wait"] = {"stage": stage.name, "seconds": round(elapsed, 1), "probe": spec.get("probe"),
                                 "passed": passed, "observed": observed, "then": spec.get("then")}
        if spec.get("then") == "regate":
            run.data["next_action"] = {"kind": "regate", "stage": stage.name, "turn": spec.get("turn")}
        self.save(run)
        run.event("wait.elapsed", stage.name, None, seconds=round(elapsed, 1), passed=passed, observed=observed,
                  then=spec.get("then"))
        log(f"waited {elapsed:.0f}s before {stage.name}: {'probe passed' if passed else 'time is up'} ({observed})")
        return True

    def dispatch(self, run: Run, stage: Stage, slot: slots.Slot | None) -> None:
        """Run what the queued run asks for: an agent attempt, or a gate-only attempt the foreman scheduled."""
        action = run.data.get("next_action") or {}
        if action.get("stage") == stage.name and action.get("kind") in ("regate", "publish"):
            self.regate(run, stage, slot, kind=action["kind"], turn=action.get("turn"))
        elif action.get("stage") == stage.name and action.get("kind") == "repair":
            self.execute(run, stage, slot, repair=action)
        else:
            self.execute(run, stage, slot)

    def acquire_slot(self, run: Run, *, abort_on_pause: bool) -> slots.Slot | None:
        return slots.acquire(
            self.home / "slots", run.id,
            count=lambda: self.cfg.max_concurrent_stages,
            poll_seconds=self.cfg.stage_poll_seconds,
            should_abort=lambda: run.cancel_requested or (abort_on_pause and run.pause_requested),
            on_wait=self.reload_config,
            sleep=self.sleep,
        )

    def recover(self, run: Run) -> None:
        """Finish the attempt a dead worker left open: reattach to its execution, or close it as orphaned."""
        open_attempt = next((a for a in reversed(run.stage_attempts(run.stage)) if a.get("ended_at") is None), None)
        if open_attempt is None:
            run.transition("resume")
            self.save(run)
            return
        stage = self.pipeline[run.stage]
        if browser.stop_recorded(open_attempt.get("browser"), grace=self.grace):
            log(f"stopped the browser {stage.name} attempt {open_attempt['n']} left behind")
        execution = open_attempt.get("execution")
        if not execution:
            reconcile_orphan(run, self.cfg)
            self.save(run)
            emit_pending(run)
            return
        try:
            receipt = executor.recover(run.dir, run.dir / execution["dir"], execution["token"],
                                       cancel_requested=lambda: run.cancel_requested,
                                       deadline_at=open_attempt.get("deadline_at"), grace=self.grace)
        except executor.Ambiguous as error:
            self.finish(run, stage, open_attempt,
                        gates.Outcome("failed", f"cannot verify the previous executor: {error}", False, "runner"),
                        None, None)
            return
        run.event("run.reattached", stage.name, open_attempt["n"], classification=receipt.classification)
        log(f"reattached to {stage.name} attempt {open_attempt['n']}: {receipt.classification}")
        slot = None
        if stage.slot_limited and receipt.classification in ("succeeded", "failed", "timeout"):
            slot = self.acquire_slot(run, abort_on_pause=False)
        try:
            self.complete(run, stage, open_attempt, receipt, slot)
        finally:
            if slot is not None:
                slot.release()

    def pause(self, run: Run) -> None:
        run.transition("pause")
        self.save(run)
        run.event("run.paused", run.stage, run.stage_record(run.stage)["attempts"] + 1)
        log(f"paused before {run.stage}")

    def cancel(self, run: Run, reason: str) -> None:
        if run.status in (CANCELLED, DONE):
            return
        run.transition("cancel", reason=reason)
        self.save(run)
        run.event("run.cancelled", reason=reason)
        notify.for_status(run, enabled=self.cfg.notify)

    # --- one attempt -------------------------------------------------------------------

    def execute(self, run: Run, stage: Stage, slot: slots.Slot | None = None, repair: dict | None = None) -> None:
        """One agent attempt: the stage skill, or a bounded repair the foreman asked for (kind `repair`)."""
        note = run.take_note()
        run.data.pop("next_action", None)
        launch = run.data.pop("next_launch", None)
        if launch and launch.get("stage") == stage.name and repair is None:
            overrides = {k: launch[k] for k in ("model", "effort", "timeout_s") if launch.get(k)}
            stage = dataclasses.replace(stage, **overrides)
        else:
            launch = None
        attempt = run.begin_attempt(stage.name, host=stage.host, model=stage.model, effort=stage.effort,
                                    kind="repair" if repair else "stage")
        n = attempt["n"]
        if launch:
            attempt["launch"] = launch
        if repair:
            attempt["repair"] = {k: repair.get(k) for k in ("instruction", "checks", "timeout_minutes", "turn")}
        if note:
            run.event("run.note_added", stage.name, n, note=note)
        attempt_dir = run.attempt_dir(stage.name, n)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        attempt["baseline"] = self.baseline(run, stage)
        result_file = run.plan_dir / f"{stage.name}-result.json"
        if result_file.exists():
            result_file.unlink()
        context_file = write_run_context(run, stage.name, n, interactive=False)
        intent.snapshot_inputs(run, stage.name, n, head=attempt["baseline"]["head"], context_file=context_file)
        bundles = provenance.skill_bundles()
        attempt["skills"] = {"resolution": bundles["resolution"], "fallback": bundles.get("fallback")}
        prompt = repair_prompt(run, stage.name, n, repair) if repair else stage.build_prompt(run, n)
        (attempt_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        try:
            sandbox = self.cfg.sandbox_for(run.data["repo"])
            # The agent gets the same writable package caches and browser settings as the runner's own commands.
            sandbox_env, cache_paths = commands.tool_caches(dict(os.environ), sandbox)
            writable = [run.report_dir, run.evidence_dir, run.scratch_dir(stage.name, n),
                        *wt.sandbox_git_dirs(run.worktree), *cache_paths]
            argv = hosts.codex_argv(
                prompt=prompt, model=stage.model, effort=stage.effort, sandbox=sandbox, worktree=run.worktree,
                writable=writable, last_message=attempt_dir / "last-message.md",
                child_agents=self.cfg.max_child_agents, agent_depth=self.cfg.max_agent_depth,
            )
            attempt["limits"] = {"child_agents": self.cfg.max_child_agents, "agent_depth": self.cfg.max_agent_depth,
                                 "enforced_by": "codex agents.max_concurrent_threads_per_session and agents.max_depth",
                                 "heavy_commands": self.cfg.max_heavy_commands}
            attempt["sandbox"] = hosts.sandbox_capabilities(sandbox, writable, run.worktree)
        except (ValueError, wt.GitError) as error:
            self.save(run)
            self.finish(run, stage, attempt, gates.Outcome("failed", f"cannot build launch command: {error}", False,
                                                           "runner"), None, None)
            return
        env = hosts.attempt_env(run_dir=run.dir, plan=run.plan, stage=stage.name, attempt=n, forward_github_token=True)
        env.update(sandbox_env)
        env["FACTORY_ROLE"] = "repair" if repair else "stage"
        if repair:
            env["FACTORY_REPAIR"] = str(sum(1 for a in run.stage_attempts(stage.name) if a.get("kind") == "repair"))
        stop = self.record_runtime(run, stage, attempt, env, bundles)
        if stop is not None:
            self.save(run)
            self.finish(run, stage, attempt, stop, None, None)
            return
        stop = self.prepare_dependencies(run, stage, n, attempt)
        if stop is not None:
            self.save(run)
            self.finish(run, stage, attempt, stop, None, None)
            return
        hosted = self.host_browser(run, stage, n, attempt, sandbox)
        env.update(browser.environment(hosted))
        token = executor.new_token()
        attempt["execution"] = {"token": token, "dir": os.path.relpath(attempt_dir, run.dir)}
        attempt["deadline_at"] = time.time() + stage.timeout_s if stage.timeout_s else None
        self.save(run)
        run.event("stage.started", stage.name, n, model=stage.model, effort=stage.effort)
        faults.point("worker.before_launch", f"worker.before_launch:{stage.name}:{n}")

        def running(state: dict) -> None:
            attempt["pid"] = state["child_pid"]
            run.save()
            run.event("process.spawned", stage.name, n, pid=state["child_pid"], guard=state.get("guard_pid"),
                      argv=argv[:2] + ["<prompt>"] + argv[3:])
            faults.point("worker.after_spawn", f"worker.after_spawn:{stage.name}:{n}")

        host_deadline = attempt["deadline_at"] - gate_reserve_s(stage.timeout_s) if attempt["deadline_at"] else None
        if repair:
            minutes = int(repair.get("timeout_minutes") or 20)
            host_deadline = time.time() + minutes * 60
            attempt["deadline_at"] = host_deadline + gate_reserve_s(stage.timeout_s)
        request = executor.Request(
            token=token, kind="repair" if repair else "stage", label=f"{stage.name}-{n}", argv=argv, cwd=str(run.worktree),
            run_dir=str(run.dir), stdout=str(attempt_dir / "stdout.jsonl"), stderr=str(attempt_dir / "stderr.log"),
            deadline_at=host_deadline, cancel_file=str(run.dir / "cancel"), grace=self.grace,
            inherit_fds=[slot.fd] if slot is not None else [],
        )
        try:
            receipt = executor.run(attempt_dir, request, env, on_running=running,
                                   cancel_requested=lambda: run.cancel_requested)
        finally:
            browser.stop(hosted, grace=self.grace)
        self.complete(run, stage, attempt, receipt, slot)

    def host_browser(self, run: Run, stage: Stage, n: int, attempt: dict, sandbox: str) -> browser.Browser | None:
        """Start the attempt's headless browser outside the agent's sandbox, where Chrome can actually run.

        The agent reaches it through AGENT_BROWSER_CDP and FACTORY_BROWSER_CDP_URL; a host without a
        browser records why and the attempt proceeds without one.
        """
        if stage.name not in ("build", "ship") or self.cfg.browser == "off":
            attempt["browser"] = {"status": "off"}
            return None
        try:
            hosted = browser.launch(home=self.home, holder=f"{run.id}:{stage.name}-{n}:browser",
                                    port_range=self.cfg.port_range,
                                    mode="workspace-write" if sandbox == "workspace-write" else "host",
                                    directory=run.attempt_dir(stage.name, n) / "browser", env=dict(os.environ))
        except browser.Unavailable as error:
            attempt["browser"] = {"status": "unavailable", "reason": str(error)}
            run.event("browser.unavailable", stage.name, n, reason=str(error))
            log(f"no hosted browser for {stage.name} attempt {n}: {error}")
            return None
        try:
            attempt["browser"] = browser.record(hosted)
            run.event("browser.hosted", stage.name, n, port=hosted.port, executable=str(hosted.executable))
        except BaseException:
            browser.stop(hosted, grace=self.grace)
            raise
        return hosted

    def complete(self, run: Run, stage: Stage, attempt: dict, receipt: executor.Receipt,
                 slot: slots.Slot | None = None) -> None:
        """Gate a finished execution and record the outcome; also the reattach path after a worker crash."""
        n = attempt["n"]
        attempt_dir = run.attempt_dir(stage.name, n)
        stream = hosts.parse_codex_stream(attempt_dir / "stdout.jsonl")
        run.event("process.exited", stage.name, n, exit_code=receipt.exit_code, timed_out=receipt.timed_out,
                  cancelled=receipt.cancelled, seconds=round(receipt.seconds, 1), spawn_error=receipt.spawn_error,
                  classification=receipt.classification)
        attempt["session_id"] = stream.thread_id
        attempt["tokens"] = stream.tokens
        attempt["usage"] = hosts.usage_record(stream)
        attempt["cost"] = pricing.estimate(pricing.load(self.home), stage.model, attempt["usage"])
        run.data["usage"] = hosts.combine_usage([a.get("usage") for a in run.data["attempts"] if a.get("host") == "codex"])

        if receipt.classification == "spawn_failed":
            outcome = gates.Outcome("failed", f"cannot launch {stage.host}: {receipt.spawn_error}", False, "runner")
            self.finish(run, stage, attempt, outcome, receipt, None)
            return
        if receipt.classification in ("lost", "guard_error", "refused"):
            reason = f"orphaned attempt: the {stage.name} execution ended without supervision ({receipt.detail})"
            self.finish(run, stage, attempt, gates.Outcome("failed", reason, True, "runner"), receipt, None)
            return
        if receipt.cancelled:
            self.finish(run, stage, attempt, gates.Outcome("cancelled", "cancelled by operator", False, "runner"),
                        receipt, None)
            return
        self.judge(run, stage, attempt, receipt, slot, stream)

    def regate(self, run: Run, stage: Stage, slot: slots.Slot | None, *, kind: str = "regate",
               turn: int | None = None) -> None:
        """A gate-only attempt the foreman scheduled: judge the stage's current state again without an agent.

        `publish` first fast-forwards the remote run branch, then judges like a regate. The attempt
        starts from the last agent attempt's baseline, so the boundary check covers the same delta.
        """
        run.data.pop("next_action", None)
        last = next((a for a in reversed(run.stage_attempts(stage.name)) if a.get("kind", "stage") == "stage"), None)
        attempt = run.begin_attempt(stage.name, host="runner", model="-", effort="-", kind=kind)
        n = attempt["n"]
        run.attempt_dir(stage.name, n).mkdir(parents=True, exist_ok=True)
        attempt["baseline"] = dict(last.get("baseline") or {}) if last else self.baseline(run, stage)
        if last and last.get("config"):
            attempt["config"] = dict(last["config"])
        attempt["decision_turn"] = turn
        attempt["deadline_at"] = time.time() + stage.timeout_s if stage.timeout_s else None
        self.save(run)
        run.event("stage.started", stage.name, n, model="-", effort="-", kind=kind)
        pre_gate = None
        if kind == "publish":
            def pre_gate(ctx: gates.GateContext) -> dict:
                operations: list[dict] = []
                detail = publication.push_head(ctx, operations)
                run.event("stage.warning", stage.name, n, warning=f"publish: {detail}")
                return {"operations": operations, "publish": detail}
        self.judge(run, stage, attempt, None, slot, hosts.CodexStream(), pre_gate=pre_gate)

    def judge(self, run: Run, stage: Stage, attempt: dict, receipt: executor.Receipt | None,
              slot: slots.Slot | None, stream: hosts.CodexStream, pre_gate=None) -> None:
        """Run the stage gate on the worktree as it is, merge it with the result file and conditions, and close."""
        n = attempt["n"]
        attempt_dir = run.attempt_dir(stage.name, n)
        ctx = gate_context(run, stage.name, n, deadline=attempt.get("deadline_at"),
                           poll_seconds=self.cfg.stage_poll_seconds, sleep=self.sleep,
                           cancel_requested=lambda: run.cancel_requested, grace=self.grace,
                           inherit_fds=[slot.fd] if slot is not None else [], home=self.home,
                           max_heavy_commands=self.cfg.max_heavy_commands, port_range=self.cfg.port_range,
                           browser=self.cfg.browser)
        gate_started = time.monotonic()
        pre: dict = {}
        try:
            if pre_gate is not None:
                pre = pre_gate(ctx)
            gate = stage.gate(ctx)
        except Exception as error:  # noqa: BLE001 - a gate crash is a retryable runner failure
            gate = gates.blocked(f"gate crashed: {type(error).__name__}: {error}", outcome="failed")
        if pre.get("operations"):
            gate.data["operations"] = [*pre["operations"], *gate.data.get("operations", [])]
        if pre.get("publish"):
            gate.data["publish"] = pre["publish"]
        gate.warnings = [*(attempt.get("setup") or {}).get("warnings", []), *gate.warnings]
        attempt["gate_warnings"] = gate.warnings
        self.record_handoff(run, stage, attempt)
        attempt["host_seconds"] = round(receipt.seconds, 1) if receipt else 0
        attempt["gate_seconds"] = round(time.monotonic() - gate_started, 1)
        attempt["checkpoints"] = ctx.checkpoint_log
        attempt["verification_seconds"] = round(sum(e["seconds"] for e in ctx.executions
                                                    if e["kind"] in ("tests", "e2e", "validation", "gauntlet")), 1)
        attempt["lease_wait_seconds"] = round(sum(e.get("lease_wait_seconds", 0) for e in ctx.executions), 1)
        attempt["gate_executions"] = len(ctx.executions)
        (attempt_dir / "gate-executions.json").write_text(json.dumps(ctx.executions, indent=2) + "\n", encoding="utf-8")
        result, result_error = gates.read_result(ctx)
        blocking, notes = conditions.evaluate(run, stage.name, attempt, None if result_error else result, dict(os.environ),
                                              gate)
        outcome = gates.merge(gate, result, result_error, blocking)
        if notes:
            outcome.warning = "; ".join(filter(None, [outcome.warning, *notes]))
        if outcome.outcome != "done" and run.cancel_requested:
            outcome = gates.Outcome("cancelled", f"cancelled by operator; gate: {outcome.reason}", False, "runner")
        elif outcome.outcome != "done" and receipt is not None:
            detail = "; ".join(stream.failures + stream.errors)
            if receipt.timed_out:
                allowance = int(stage.timeout_s - gate_reserve_s(stage.timeout_s))
                outcome = gates.Outcome("timeout", f"timed out after its {allowance}s allowance; {outcome.reason}",
                                        outcome.retryable, "runner", code=outcome.code, conditions=outcome.conditions)
            elif receipt.exit_code not in (0, None):
                reason = f"{stage.host} exited {receipt.exit_code}; {outcome.reason}"
                if detail:
                    reason += f" ({detail[:300]})"
                outcome = gates.Outcome("failed", reason, outcome.retryable, outcome.source, code=outcome.code,
                                        conditions=outcome.conditions)
        elif outcome.outcome == "done":
            if receipt is not None and receipt.exit_code not in (0, None):
                note = f"{stage.host} exited {receipt.exit_code} but the gate passed"
                outcome.warning = f"{outcome.warning}; {note}" if outcome.warning else note
            outcome = self.on_pass(run, stage, gate, outcome)
        self.finish(run, stage, attempt, outcome, receipt, gate, result)

    def prepare_dependencies(self, run: Run, stage: Stage, n: int, attempt: dict) -> gates.Outcome | None:
        """Run the contract's setup in the worktree before an agent that builds or verifies code starts.

        Reused while the dependency files are unchanged; a missing or invalid contract is left to the gate to report.
        """
        if stage.name not in ("build", "ship"):
            return None
        ctx = gate_context(run, stage.name, n)
        try:
            ctx.approved = intent.load_approved(run.dir, ctx.intent_sha256)
            contract = records.load_contract(gates.contract_path(ctx))
        except (intent.IntentError, records.RecordError):
            return None
        data: dict = {}
        started = time.monotonic()
        result = gates.run_setup(ctx, contract, data)
        attempt["setup"] = {**data.get("setup", {}), "seconds": round(time.monotonic() - started, 1),
                            "decisions": ctx.checkpoint_log, "warnings": ctx.warnings}
        if result is None:
            return None
        return gates.Outcome("blocked", f"before launching {stage.name}: {result.reason}", result.retryable, "runner",
                             code=result.code)

    def baseline(self, run: Run, stage: Stage) -> dict:
        """Where this attempt starts, so its gate judges the attempt's complete Git delta."""
        baseline = {"head": wt.head(run.worktree), "branch": wt.current_branch(run.worktree)}
        if stage.name == "scope-review":
            index, _ = gates.highest_index(run.plan_dir, "spec-review")
            baseline["spec_review_index"] = index
        if stage.name == "build":
            record = run.stage_record("build")
            if not record.get("start_sha"):
                record["start_sha"] = baseline["head"]
            baseline["build_start_sha"] = record["start_sha"]
        return baseline

    def on_pass(self, run: Run, stage: Stage, gate: gates.GateResult, outcome: gates.Outcome) -> gates.Outcome:
        if stage.name != "scope-review":
            return outcome
        ctx = gate_context(run, stage.name, run.stage_record(stage.name)["attempts"])
        paths = [p for p in wt.changed_paths(run.worktree) if gates.scope_review_allowed(ctx, p)]
        try:
            sha = wt.commit_paths(run.worktree, paths, f"docs(scope-review): record refined decisions for {run.plan}")
        except wt.GitError as error:
            return gates.Outcome("failed", f"commit of reviewed scope artifacts failed: {error}", True, "runner")
        run.event("stage.committed", stage.name, commit=sha, paths=paths, nothing_to_commit=sha is None)
        faults.point("worker.after_stage_commit", f"worker.after_stage_commit:{stage.name}")
        return outcome

    def finish(self, run: Run, stage: Stage, attempt: dict, outcome: gates.Outcome,
               receipt: executor.Receipt | None, gate: gates.GateResult | None, result: dict | None = None) -> None:
        """Close the attempt and save it undecided; the loop chooses its transition once the slot is free.

        The attempt's own events wait on the attempt (`closed_events`) and go out with the transition,
        so a crash between closing and deciding repeats neither the attempt nor its events.
        """
        n = attempt["n"]
        attempt_dir = run.attempt_dir(stage.name, n)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        record = {"outcome": outcome.outcome, "reason": outcome.reason, "retryable": outcome.retryable,
                  "source": outcome.source, "warning": outcome.warning,
                  "gate": gate.to_json() if gate else None, "skill_result": result}
        gates_json = attempt_dir / "gate.json"
        gates_json.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
        if gate is not None:
            self.record_artifacts(run, gate)
        run.finish_attempt(attempt, outcome=outcome.outcome, reason=outcome.reason, retryable=outcome.retryable,
                           source=outcome.source, tokens=attempt.get("tokens", 0),
                           exit_code=receipt.exit_code if receipt else None, session_id=attempt.get("session_id"),
                           warning=outcome.warning, code=outcome.code, conditions=outcome.conditions)
        attempt["fingerprint"] = progress_fingerprint(run)
        pending: list = [["gate.evaluated", stage.name, n, {"passed": bool(gate and gate.passed),
                                                             "reason": gate.reason if gate else None}]]
        if outcome.warning:
            pending.append(["stage.warning", stage.name, n, {"warning": outcome.warning}])
        pending.append(["stage.finished", stage.name, n, {
            "outcome": outcome.outcome, "reason": outcome.reason, "retryable": outcome.retryable,
            "source": outcome.source, "code": outcome.code, "conditions": outcome.conditions,
            "tokens": attempt.get("tokens", 0)}])
        attempt["closed_events"] = pending
        self.save(run)
        faults.point("worker.after_attempt_close", f"worker.after_attempt_close:{stage.name}:{n}")

    def decide_next(self, run: Run, attempt: dict) -> None:
        """Choose and apply the closed attempt's transition: the foreman's decision, or `decide()` as the fallback.

        Runs with no slot held. Everything the choice needs is saved before the transition, so a crash
        after the foreman's turn reuses its decision instead of asking again.
        """
        stage = self.pipeline[attempt["stage"]]
        n = attempt["n"]
        outcome = gates.Outcome(attempt["outcome"], attempt.get("reason"), bool(attempt.get("retryable")),
                                attempt.get("source") or "runner", warning=attempt.get("warning"),
                                code=attempt.get("code"), conditions=list(attempt.get("conditions") or []))
        pending: list = list(attempt.pop("closed_events", []))
        note = run.take_note()
        if note:
            pending.append(["run.note_added", stage.name, n, {"note": note}])
        decision = None
        if self.cfg.foreman != "off" and outcome.outcome != "cancelled":
            event = foreman.attempt_event(run, stage.name, attempt, outcome)
            turn = foreman.load_turn(run, attempt)
            if turn is None:
                turn = foreman.consult(run, self.cfg, event, home=self.home, grace=self.grace,
                                       cancel_requested=lambda: run.cancel_requested)
                foreman.record(run, attempt, turn, event)
                attempt["closed_events"] = pending
                self.save(run)
                attempt.pop("closed_events", None)
            pending += foreman.turn_events(attempt, turn)
            if self.cfg.foreman == "codex":
                decision = turn.decision
        faults.point("worker.after_foreman_turn", f"worker.after_foreman_turn:{stage.name}:{n}")
        if decision is not None and turn.extra.get("boundary"):
            # The turn committed outside .dev/: that is a boundary violation, whatever it decided.
            decision = records.validate_decision({
                "schema": records.DECISION_SCHEMA, "action": "park", "summary": "foreman committed outside .dev/",
                "rationale": turn.extra["boundary"], "park": {
                    "reason": f"boundary.violation: {turn.extra['boundary']}",
                    "operator_action": f"inspect the commit in {run.worktree}, then `factory retry {run.id}`"}})
        apply_decision(run, attempt, self.cfg, pending, decision=decision)
        if run.status == DONE and run.data.get("overrides") and not run.data.get("overrides_noted"):
            operations: list[dict] = []
            ctx = gate_context(run, stage.name, n, sleep=self.sleep, cancel_requested=lambda: run.cancel_requested,
                               grace=self.grace, home=self.home)
            try:
                detail = publication.note_overrides(ctx, run.data["overrides"], operations)
            except Exception as error:  # noqa: BLE001 - the note is accountability, never a reason to fail a done run
                detail = f"could not note the overrides: {type(error).__name__}: {error}"
            run.data["overrides_noted"] = detail
            run.data.setdefault("operations", []).extend(operations)
            pending.append(["stage.warning", stage.name, n, {"warning": f"overrides: {detail}"}])
            log(f"overrides: {detail}")
        faults.point("worker.before_transition_save", f"worker.before_transition_save:{stage.name}:{n}")
        self.save(run)
        faults.point("worker.after_transition_save", f"worker.after_transition_save:{stage.name}:{n}")
        emit_pending(run)
        if run.status in PARKED:
            notify.for_status(run, enabled=self.cfg.notify)

    def record_runtime(self, run: Run, stage: Stage, attempt: dict, env: dict,
                       bundles: dict | None = None) -> gates.Outcome | None:
        """Write the attempt's runtime manifest; an outcome when the attempt must not launch."""
        contract, contract_sha = None, None
        try:
            document = intent.load_approved(run.dir, (run.data.get("intent") or {}).get("sha256"))
            contract_sha = document.get("contract_sha256")
            path = intent.approved_contract(run.dir, document)
            contract = records.load_contract(path) if path else None
        except (intent.IntentError, records.RecordError):
            pass
        bundles = bundles or provenance.skill_bundles()
        manifest = provenance.attempt_manifest(
            run_id=run.id, stage=stage.name, attempt=attempt["n"], model=stage.model, effort=stage.effort,
            timeout_s=stage.timeout_s, cfg=self.cfg, repo=run.data["repo"], contract=contract,
            contract_sha256=contract_sha, env=env, forwarded_github_token="GH_TOKEN" in env and "GH_TOKEN" not in os.environ,
            bundles=bundles)
        intent.write_json(run.attempt_dir(stage.name, attempt["n"]) / "runtime.json", manifest)
        attempt["config"] = {"sha256": manifest["config"]["sha256"], "semantic": manifest["config"]["semantic"]}
        attempt["runtime"] = {"skills_id": bundles["skills_id"], "runner": manifest["runner"]["content_sha256"],
                              "codex": manifest["hosts"].get("codex")}
        previous = next((a for a in reversed(run.data["attempts"][:-1]) if a.get("config")), None)
        if previous is not None and previous["config"]["sha256"] != attempt["config"]["sha256"]:
            run.event("config.changed", stage.name, attempt["n"], previous=f"{previous['stage']}-{previous['n']}",
                      semantic=attempt["config"]["semantic"], applies="from this attempt; live resource controls "
                      "(slots, polling, heartbeat, notifications) apply immediately")
        if bundles.get("fallback"):
            note = (f"{bundles['fallback']}; this attempt reads the generated skills at {bundles['generated']['path']} "
                    "directly (reinstall with `codex plugin add factory@nurbot` to use the plugin)")
            run.event("skills.fallback", stage.name, attempt["n"], reason=note, path=bundles["generated"]["path"])
        return None

    def record_handoff(self, run: Run, stage: Stage, attempt: dict) -> None:
        """What the attempt produced: output hashes and, when the spec changed, its decision delta."""
        n = attempt["n"]
        try:
            head = wt.head(run.worktree)
        except wt.GitError:
            head = None
        intent.snapshot_outputs(run, stage.name, n, head=head)
        audit = ""
        for pattern in ("spec-review_*.md", "implementation-notes.md", "review_*.md"):
            for path in run.plan_dir.glob(pattern):
                audit += path.read_text(encoding="utf-8", errors="replace") + "\n"
        delta = intent.decision_delta(run, stage.name, n, auto_decided=intent.auto_decisions(audit))
        if delta is not None:
            attempt["decision_delta"] = f"intent/deltas/{stage.name}-{n}.json"

    def record_artifacts(self, run: Run, gate: gates.GateResult) -> None:
        data = gate.data
        for key in ("spec_review", "notes", "e2e_report", "review", "pr_body"):
            if data.get(key):
                run.data["artifacts"][key] = data[key]
        if data.get("pr"):
            run.data["pr"].update({k: v for k, v in data["pr"].items() if v is not None})
        if gate.passed and data.get("completion"):
            run.data["completion"] = data["completion"]
        if data.get("operations"):
            run.data.setdefault("operations", []).extend(data["operations"])


def apply_decision(run: Run, attempt: dict, cfg: config.Config, pending: list | None = None, *,
                   decision: dict | None = None) -> str:
    """Move a running run to its next status after an attempt; returns the action taken.

    With `pending`, the transition's events are stored on the attempt (after any events
    already in `pending`) instead of written, so the caller persists the decision and its
    history in one save and then calls `emit_pending`. Without it, events are written now.
    With `decision`, the foreman's validated decision is applied under the runner's caps and
    hard stops; a decision the runner refuses is recorded and `decide()` chooses instead.
    """
    semantic = (attempt.get("config") or {}).get("semantic") or {}
    retry_budget = semantic.get("max_retries", cfg.max_retries)
    if retry_budget < run.data["retries"]["budget"]:
        run.data["retries"]["budget"] = retry_budget
    stage = attempt["stage"]
    planned: list = [] if pending is None else pending
    target = stage
    operator_action = None
    if decision is not None:
        action, reason, rejection = enforce(run, attempt, decision, cfg)
        if rejection:
            planned.append(["foreman.rejected", stage, attempt["n"], {"action": decision["action"], "why": rejection}])
            attempt.setdefault("foreman", {})["rejected"] = rejection
            action, reason = decide(run, attempt, stop_on_repeated_reason=cfg.stop_on_repeated_reason)
        else:
            attempt.setdefault("foreman", {})["applied"] = action
            for entry in reversed(run.data.get("decisions") or []):
                if entry.get("stage") == stage and entry.get("attempt") == attempt["n"]:
                    entry["applied"] = True
                    break
            now = utc_now()
            for item in decision.get("resolve_conditions") or []:
                condition = next((c for c in run.data.get("conditions") or [] if c.get("id") == item["id"]), None)
                if condition is None or condition.get("status") == "resolved":
                    continue
                observed, detail = conditions.recheck(condition, dict(os.environ))
                if observed is False:
                    planned.append(["stage.warning", stage, attempt["n"], {
                        "warning": f"{item['id']} stays open: the runner's re-check found it {detail}"}])
                    continue
                conditions.resolve(condition, attempt=attempt["n"], by="foreman", evidence=list(item["evidence"]),
                                   now=now)
            if action == "override":
                override = decision["override"]
                entry = {"stage": stage, "attempt": attempt["n"], "gate_code": override["gate_code"],
                         "justification": override["justification"], "evidence": list(override.get("evidence") or []),
                         "turn": (attempt.get("foreman") or {}).get("turn"), "at": now,
                         "outcome": attempt.get("outcome"), "reason": attempt.get("reason")}
                run.data.setdefault("overrides", []).append(entry)
                planned.append(["override.recorded", stage, attempt["n"], {
                    "gate_code": override["gate_code"], "justification": override["justification"]}])
                for condition in conditions.open_for(run, stage):
                    if condition["code"] == override["gate_code"] or condition["id"] in (attempt.get("conditions") or []):
                        conditions.resolve(condition, attempt=attempt["n"], by="foreman",
                                           evidence=[override["justification"], *entry["evidence"]], now=now)
                action = "stage_passed"
            if action in ("retry", "launch"):
                target = decision["stage"]
                heading = f"after {stage} attempt {attempt['n']}"
                remember_guidance(run, target, heading, decision["guidance"])
                overrides = {k: decision[k] for k in ("model", "effort", "timeout_s") if decision.get(k)}
                run.data["next_launch"] = {"stage": target, "turn": (attempt.get("foreman") or {}).get("turn"),
                                           **overrides}
            elif action in ("regate", "publish"):
                target = decision.get("stage") or ("ship" if action == "publish" else stage)
                run.data["next_action"] = {"kind": action, "stage": target,
                                           "turn": (attempt.get("foreman") or {}).get("turn")}
            elif action == "repair":
                target = decision["stage"]
                spec = decision["repair"]
                if decision.get("guidance"):
                    remember_guidance(run, target, f"after {stage} attempt {attempt['n']} (repair)", decision["guidance"])
                run.data["next_action"] = {"kind": "repair", "stage": target, "instruction": spec["instruction"],
                                           "checks": spec.get("checks") or [],
                                           "timeout_minutes": spec.get("timeout_minutes") or 20,
                                           "turn": (attempt.get("foreman") or {}).get("turn")}
            elif action == "wait":
                spec = decision["wait"]
                used = float((run.data.get("waits") or {}).get(stage, 0))
                seconds = min(int(spec["seconds"]), max(1, int(cfg.max_wait_minutes * 60 - used)))
                run.data["wait"] = {"stage": stage, "seconds": seconds, "probe": spec["probe"], "then": spec["then"],
                                    "started": time.time(), "until": time.time() + seconds,
                                    "turn": (attempt.get("foreman") or {}).get("turn")}
            elif action == "park" and decision["action"] == "park":
                operator_action = decision["park"]["operator_action"]
            elif action == "park":
                operator_action = f"factory retry {run.id} --reset-budget"
            elif action == "rescope":
                operator_action = f"factory retry {run.id} --rescope"
    else:
        action, reason = decide(run, attempt, stop_on_repeated_reason=cfg.stop_on_repeated_reason)
    if action in ("retry", "launch") and run.retry_needed(target):
        try:
            run.consume_retry()
        except BudgetExhausted as error:
            if decision is None:
                action = "exhausted"
            else:
                # Under the foreman the budget parks instead of cancelling: `factory retry --reset-budget` reopens it.
                action, reason = "park", f"{error}; last reason: {attempt.get('reason')}"
                run.data.pop("next_launch", None)
                operator_action = f"factory retry {run.id} --reset-budget"
    if action == "stage_passed":
        target = run.transition("stage_passed")
        if target == DONE:
            planned.append(["run.done", stage, attempt["n"], {"pr": run.data["pr"].get("url")}])
        else:
            planned.append(["run.queued", run.stage, None, {"previous": stage}])
    elif action == "retry":
        run.transition("retry")
        planned.append(["retry.scheduled", stage, attempt["n"] + 1, {
            "reason": reason, "used": run.data["retries"]["used"], "budget": run.data["retries"]["budget"]}])
        planned.append(["run.queued", stage, attempt["n"] + 1, {}])
    elif action == "launch":
        run.transition("launch", stage=target)
        planned.append(["retry.scheduled", target, run.stage_record(target)["attempts"] + 1, {
            "reason": reason, "used": run.data["retries"]["used"], "budget": run.data["retries"]["budget"],
            "previous": stage}])
        planned.append(["run.queued", target, run.stage_record(target)["attempts"] + 1, {"previous": stage}])
    elif action in ("regate", "publish", "repair"):
        run.transition(action, stage=target)
        planned.append([f"{action}.scheduled", target, run.stage_record(target)["attempts"] + 1,
                        {"reason": reason, "previous": stage}])
        planned.append(["run.queued", target, run.stage_record(target)["attempts"] + 1, {"kind": action}])
    elif action == "wait":
        run.transition("wait")
        spec = run.data["wait"]
        planned.append(["wait.scheduled", stage, attempt["n"], {"seconds": spec["seconds"], "probe": spec["probe"],
                                                                "then": spec["then"], "reason": reason}])
        planned.append(["run.queued", stage, run.stage_record(stage)["attempts"] + 1, {"kind": "wait"}])
    elif action == "exhausted":
        run.transition("exhausted", reason=reason)
        planned.append(["run.cancelled", stage, attempt["n"], {"reason": reason}])
    elif action in ("park", "rescope"):
        run.transition("park", reason=reason, operator_action=operator_action)
        planned.append(["run.needs_human", stage, attempt["n"], {"reason": reason, "operator_action": operator_action,
                                                                  "rescope": action == "rescope"}])
    elif action == "cancel":
        run.transition("cancel", reason=reason)
        planned.append(["run.cancelled", stage, attempt["n"], {"reason": reason}])
    if pending is None:
        for name, event_stage, n, data in planned:
            run.event(name, event_stage, n, **data)
    else:
        attempt["transition"] = {"id": uuid.uuid4().hex, "action": action, "status": run.status,
                                 "stage": run.stage, "events": planned}
    return action


def probe(spec: dict) -> tuple[bool, str]:
    """Whether a wait's typed probe passes now, and what was observed. Never a command from the decision."""
    kind = spec.get("kind") or "none"
    value = spec.get("value")
    if kind == "env":
        present = bool(os.environ.get(value or ""))
        return present, f"{value} is {'set' if present else 'unset'}"
    if kind == "gh_auth":
        try:
            result = subprocess.run([config.binary("gh"), "auth", "status"], capture_output=True, text=True,
                                    timeout=30, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            return False, f"gh auth status: {error}"
        return result.returncode == 0, (result.stdout or result.stderr).strip().splitlines()[0:1][0] if (
            result.stdout or result.stderr).strip() else f"gh auth status exited {result.returncode}"
    if kind == "url":
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(urllib.request.Request(value, method="GET"), timeout=10) as response:
                return response.status < 400, f"{value} answered {response.status}"
        except urllib.error.HTTPError as error:
            return False, f"{value} answered {error.code}"
        except (urllib.error.URLError, OSError, ValueError) as error:
            return False, f"{value}: {error}"
    return False, "no probe; waiting the full time"


def progress_fingerprint(run: Run) -> str | None:
    """What an attempt left behind: the commit and every plan file, so an unchanged retry is visible."""
    try:
        head = wt.head(run.worktree)
    except wt.GitError:
        return None
    files = intent.manifest(intent.plan_files(run.plan_dir), run.plan_dir) if run.plan_dir.is_dir() else {}
    return records.canonical_sha256([head, files])


def emit_pending(run: Run) -> int:
    """Write every recorded transition event missing from events.jsonl; returns how many were written.

    A worker that died between saving a transition and logging it leaves the saved record as
    the authority, so the log is repaired from it instead of replaying any side effect.
    """
    recorded = [a for a in run.data["attempts"] if a.get("transition")]
    if not recorded:
        return 0
    seen = {e.get("data", {}).get("transition") for e in events.read(run.dir)}
    written = 0
    for attempt in recorded:
        transition = attempt["transition"]
        for index, (name, stage, n, data) in enumerate(transition["events"]):
            marker = f"{transition['id']}#{index}"
            if marker in seen:
                continue
            run.event(name, stage, n, **data, transition=marker)
            written += 1
    return written


def reconcile_orphan(run: Run, cfg: config.Config) -> str:
    """Close an attempt a dead worker left open with no execution to reattach to. Caller saves, then emit_pending."""
    if run.status != RUNNING:
        return "none"
    if run.undecided_attempt() is not None:
        return "undecided"
    open_attempt = next((a for a in reversed(run.stage_attempts(run.stage)) if a.get("ended_at") is None), None)
    if open_attempt is None:
        run.transition("resume")
        return "requeued"
    if run.cancel_requested:
        run.finish_attempt(open_attempt, outcome="cancelled", reason="worker died; cancel was requested",
                           retryable=False, source="runner")
    else:
        run.finish_attempt(open_attempt, outcome="failed", reason="orphaned attempt: the worker died mid-stage",
                           retryable=True, source="runner")
    pending = [["stage.finished", run.stage, open_attempt["n"], {
        "outcome": open_attempt["outcome"], "reason": open_attempt["reason"], "orphaned": True}]]
    return apply_decision(run, open_attempt, cfg, pending)


def preload() -> list[str]:
    """Import every runner module now, so a code change on disk during the run cannot tear this process.

    Several modules import each other lazily inside functions; without this, a worker that started before an
    edit would combine old modules with new ones at the first gate and crash on a name the old ones lack.
    """
    import importlib
    import pkgutil

    import runner
    names = sorted(f"runner.{info.name}" for info in pkgutil.iter_modules(runner.__path__) if info.name != "tests")
    for name in names:
        importlib.import_module(name)
    return names


def main(run_id: str) -> int:
    home = config.factory_home()
    log(f"worker {os.getpid()} starting for {run_id}")
    preload()
    try:
        return Worker(home, run_id).run()
    except config.ConfigError as error:
        log(f"config error: {error}")
        return 2
    except Exception:  # noqa: BLE001 - leave a trace; lock state marks the run for `factory resume`
        log("worker crashed:\n" + traceback.format_exc())
        return 1
