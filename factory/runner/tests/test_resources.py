"""Nested work budgets and truthful usage: categories, leases, ports, no-progress, and in-flight ceilings."""

import contextlib
import io
import json
import os
import signal
import time
import unittest
from pathlib import Path

from runner import cli, hosts, pricing, resources, supervise
from runner.model import Run
from runner.tests.helpers import CONTRACT, E2E_DRIVER_PY, FactoryTestCase, build_step, git, happy_scenario, make_repo
from runner.worker import Worker

REAL_SHIP = {"input_tokens": 32408004, "cached_input_tokens": 31770198, "output_tokens": 128833}


class UsageTests(unittest.TestCase):
    def stream(self, *lines: dict) -> hosts.CodexStream:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stdout.jsonl"
            path.write_text("".join(json.dumps(line) + "\n" for line in lines))
            return hosts.parse_codex_stream(path)

    def test_categories_are_parts_of_their_totals_and_child_usage_stays_unknown(self):
        stream = self.stream(
            {"type": "turn.completed", "usage": {"input_tokens": 1000, "cached_input_tokens": 800,
                                                 "cache_write_input_tokens": 150, "output_tokens": 100,
                                                 "reasoning_output_tokens": 40}},
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "status": "completed"}},
            {"type": "item.completed", "item": {"type": "collab_tool_call", "tool": "spawn_agent", "status": "failed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 500, "cached_input_tokens": 500, "output_tokens": 50}})
        usage = hosts.usage_record(stream)
        self.assertEqual((usage["input"], usage["cached_input"], usage["uncached_input"], usage["cache_write_input"]),
                         (1500, 1300, 200, 150))
        self.assertEqual((usage["output"], usage["reasoning_output"], usage["total"]), (150, 40, 1650))
        self.assertEqual(stream.tokens, 1650, "the raw total stays input plus output, never plus its parts")
        self.assertEqual((usage["child_agents"], usage["child_usage"], stream.child_agents_failed), (1, "unknown", 1))
        combined = hosts.combine_usage([usage, hosts.usage_record(self.stream({"type": "turn.completed"}))])
        self.assertEqual((combined["total"], combined["reported_attempts"], combined["unreported_attempts"]), (1650, 1, 1))
        self.assertEqual(combined["scope_usage"], "unknown")

    def test_the_recorded_real_ship_attempt_is_not_priced_as_uncached_input(self):
        usage = hosts.usage_record(self.stream({"type": "turn.completed", "usage": REAL_SHIP}))
        self.assertEqual(usage["total"], 32536837)
        self.assertEqual(pricing.describe(usage), "32.5M tokens: 31.8M cached input, 638K uncached input "
                                                  "(0 cache writes), 129K output (0 reasoning)")
        self.assertEqual(pricing.estimate(None, "m", usage)["amount"], None)
        table = {"schema": "factory.pricing/1", "version": "test", "currency": "USD", "models": {"m": {
            "input_per_mtok": 1.0, "cached_input_per_mtok": 0.1, "cache_write_input_per_mtok": 1.0, "output_per_mtok": 10.0}}}
        estimate = pricing.estimate(table, "m", usage)
        self.assertAlmostEqual(estimate["amount"], (31770198 * 0.1 + 637806 * 1.0 + 128833 * 10.0) / 1e6, places=3)
        self.assertLess(estimate["amount"], 32408004 * 1.0 / 1e6 / 5, "cached input must not be priced as uncached")
        self.assertIsNone(pricing.estimate(table, "unpriced", usage)["amount"])


class WorkerUsageTests(FactoryTestCase):
    def call(self, *argv: str) -> str:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            cli.main(list(argv))
        return stdout.getvalue()

    def test_show_displays_categories_unknowns_and_durations(self):
        scenario = happy_scenario()
        scenario["scope-review"][0]["usage"] = REAL_SHIP
        scenario["build"][0]["usage"] = "omit"
        self.scenario(scenario)
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["attempts"][1]["usage"]["reported"], False)
        self.assertEqual(data["usage"]["unreported_attempts"], 1)
        shown = self.call("show", run.id)
        self.assertIn("usage      32.5M tokens: 31.8M cached input, 639K uncached input", shown)
        self.assertIn("scope usage unknown; estimated cost unknown (no pricing table", shown)
        self.assertRegex(shown, r"build-1  done  \S+ \(queue \d+s, agent \d+s, gate \d+s of which verification \d+s")

    def test_no_progress_stops_retries_with_the_evidence(self):
        broken = build_step()
        del broken["files"][".dev/{plan}/scenario-map.json"]
        broken["run"] = [["git", "add", "-A"], ["sh", "-c", "git diff --cached --quiet || git commit --quiet -m wip"]]
        scenario = happy_scenario()
        scenario["build"] = [broken]
        self.scenario(scenario)
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        builds = [a for a in data["attempts"] if a["stage"] == "build"]
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(len(builds), 2)
        self.assertIn("no progress: build attempts 1 and 2 both failed with evidence.map_missing and left the commit "
                      "and plan files unchanged", data["human"]["reason"])
        self.assertEqual(data["retries"]["used"], 1)

    def test_a_failing_host_exit_keeps_the_gate_code_so_no_progress_still_stops(self):
        broken = build_step()
        del broken["files"][".dev/{plan}/scenario-map.json"]
        broken["exit"] = 1
        scenario = happy_scenario()
        scenario["build"] = [broken]
        self.scenario(scenario)
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        builds = [a for a in data["attempts"] if a["stage"] == "build"]
        self.assertEqual([(a["outcome"], a["code"]) for a in builds],
                         [("failed", "evidence.map_missing"), ("failed", "evidence.map_missing")])
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("no progress", data["human"]["reason"])
        self.assertEqual(data["retries"]["used"], 1)

    def test_a_changing_retry_is_not_stopped_as_no_progress(self):
        attempts = []
        for index in range(3):
            step = build_step()
            del step["files"][".dev/{plan}/scenario-map.json"]
            step["files"][f"notes-{index}.md"] = f"attempt {index}\n"
            attempts.append(step)
        attempts.append(build_step())
        scenario = happy_scenario()
        scenario["build"] = attempts
        self.scenario(scenario)
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(len([a for a in data["attempts"] if a["stage"] == "build"]), 4)

    def test_reported_usage_does_not_stop_an_attempt(self):
        (self.home / "config.json").write_text(json.dumps({**self.fast_config, "max_tokens_per_run": 5000}))
        scenario = happy_scenario()
        scenario["scope-review"][0]["early_usage"] = {"input_tokens": 9000, "output_tokens": 100}
        self.scenario(scenario)
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done")
        self.assertGreater(data["attempts"][0]["tokens"], 5000)


class ConcurrencyTests(FactoryTestCase):
    fast_config = {**FactoryTestCase.fast_config, "max_concurrent_stages": 4, "max_heavy_commands": 1,
                   "max_child_agents": 2}

    def repo_with(self, contract: dict, name: str) -> Path:
        repo = make_repo(self.root, name)
        (repo / ".factory" / "contract.json").write_text(json.dumps(contract))
        git(repo, "commit", "--quiet", "-am", "contract")
        git(repo, "push", "--quiet")
        return repo

    def start(self, run: Run) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["resume", run.id, "--detach"]), 0)

    def test_four_concurrent_runs_obey_nested_agent_and_heavy_command_limits(self):
        log = self.root / "heavy.log"
        record = f"echo start $(python3 -c 'import time; print(time.time())') >> {log}; sleep 0.4; " \
                 f"echo end $(python3 -c 'import time; print(time.time())') >> {log}"
        contract = {**CONTRACT, "validation": [{"id": "heavy", "run": ["sh", "-c", record]}]}
        self.scenario(happy_scenario())
        runs = [self.queued_run(plan=f"heavy-{index}", repo=self.repo_with(contract, f"heavy-{index}")) for index in range(4)]
        for run in runs:
            self.start(run)
        for run in runs:
            self.assertEqual(self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=180)["status"], "done")
        stamps = [line.split() for line in log.read_text().splitlines()]
        depth = peak = 0
        for kind, _ in sorted(stamps, key=lambda item: (float(item[1]), item[0] == "start")):
            depth += 1 if kind == "start" else -1
            peak = max(peak, depth)
        self.assertEqual(peak, 1, "two heavy commands ran at once")
        self.assertEqual(len(stamps), 8, "each run validates once; ship reuses the unchanged tree's checkpoint")
        codex = [c for c in self.stub_calls("codex") if c["argv"][0] == "exec"]
        self.assertTrue(all("agents.max_concurrent_threads_per_session=2" in c["argv"] for c in codex))
        waits = [a.get("lease_wait_seconds", 0) for run in runs for a in Run.load(run.dir).data["attempts"]]
        self.assertGreater(max(waits), 0)

    def test_a_heavy_lease_survives_worker_death_and_is_released_after_the_command(self):
        contract = {**CONTRACT, "validation": [{"id": "slow", "run": ["sleep", "3"]}]}
        self.scenario(happy_scenario())
        run = self.queued_run(repo=self.repo_with(contract, "crash-lease"))
        self.start(run)
        self.wait_for(lambda: resources.holders(self.home, "heavy") and any(
            "validation-slow" in (holder or "") for holder in resources.holders(self.home, "heavy").values()),
            60, "heavy lease for validation")
        os.kill(supervise.read_pid(run.dir), signal.SIGKILL)
        self.wait_for(lambda: not supervise.worker_alive(run.dir), 10, "worker death")
        self.assertTrue(resources.holders(self.home, "heavy"), "the lease must outlive the worker while the command runs")
        self.wait_for(lambda: resources.holders(self.home, "heavy") == {}, 20, "lease release after the command")

    def test_concurrent_worktrees_get_distinct_ports_and_service_data(self):
        seen = self.root / "ports.log"
        driver = E2E_DRIVER_PY.replace(
            'out = os.environ["FACTORY_E2E_OUT"]',
            'out = os.environ["FACTORY_E2E_OUT"]\n'
            f'open({str(seen)!r}, "a").write(os.environ["FACTORY_PORT_API"] + " " + os.environ["FACTORY_SERVICE_DATA"] + "\\n")\n'
            'import time\ntime.sleep(1)')
        contract = {**CONTRACT, "ports": ["api"]}
        runs = []
        for index in range(2):
            scenario = happy_scenario()
            scenario["build"][0]["files"]["e2e/run.py"] = driver
            self.scenario(scenario)
            runs.append(self.queued_run(plan=f"ports-{index}", repo=self.repo_with(contract, f"ports-{index}")))
        for run in runs:
            self.start(run)
        for run in runs:
            self.assertEqual(self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=180)["status"], "done")
        lines = [line.split() for line in seen.read_text().splitlines()]
        for phase in ("/attempts/build-1/",):
            concurrent = [(port, data) for port, data in lines if phase in data]
            self.assertEqual(len(concurrent), 2, lines)
            self.assertEqual(len({port for port, _ in concurrent}), 2, f"a port was shared concurrently: {concurrent}")
        self.assertEqual(len({data for _, data in lines}), len(lines), "service data directories were shared")
        self.assertEqual(resources.holders(self.home, "ports"), {})


if __name__ == "__main__":
    unittest.main()
