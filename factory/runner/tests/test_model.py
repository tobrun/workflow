import json
import unittest
from pathlib import Path
from unittest import mock

from runner import config
from runner.model import (CANCELLED, DONE, NEW, QUEUED, RUNNING, SCOPING, STATUSES, TRANSITIONS,
                          InvalidTransition, Run, SchemaError, decide)
from runner.tests.helpers import FactoryTestCase
from runner.worker import apply_decision

ACTIONS = sorted({action for _, action in TRANSITIONS})


class ModelTestCase(FactoryTestCase):
    def new_run(self, status: str = NEW, stage: str = "scope") -> Run:
        run = Run.create(self.home / "runs", repo="/tmp/repo", request="Add a thing", plan="thing")
        run.data["status"] = status
        run.data["stage"] = stage
        run.save()
        return run

    def attempt(self, run: Run, outcome: str, reason: str | None = "boom", retryable: bool = True,
                tokens: int = 10) -> dict:
        attempt = run.begin_attempt(run.stage, host="codex", model="m", effort="e")
        run.finish_attempt(attempt, outcome=outcome, reason=reason, retryable=retryable, source="gate", tokens=tokens)
        return attempt


class TransitionTests(ModelTestCase):
    def test_every_valid_transition(self):
        for (status, action), target in TRANSITIONS.items():
            with self.subTest(status=status, action=action):
                stage = "ship" if action == "stage_passed" else "build"
                run = self.new_run(status, stage)
                result = run.transition(action, reason="why")
                if target == "next":
                    self.assertEqual(result, DONE)
                else:
                    self.assertEqual(result, target)
                    self.assertEqual(run.status, target)

    def test_stage_passed_advances_to_next_stage(self):
        run = self.new_run(RUNNING, "scope-review")
        self.assertEqual(run.transition("stage_passed"), QUEUED)
        self.assertEqual(run.stage, "build")

    def test_every_invalid_transition_leaves_state_untouched(self):
        for status in STATUSES:
            for action in ACTIONS:
                if (status, action) in TRANSITIONS:
                    continue
                with self.subTest(status=status, action=action):
                    run = self.new_run(status, "build")
                    before = json.dumps(run.data, sort_keys=True)
                    with self.assertRaises(InvalidTransition):
                        run.transition(action)
                    self.assertEqual(json.dumps(run.data, sort_keys=True), before)
                    on_disk = json.loads((run.dir / "run.json").read_text())
                    self.assertEqual(on_disk["status"], status)

    def test_needs_human_records_reason(self):
        run = self.new_run(RUNNING, "build")
        run.transition("park", reason="premise invalidated")
        self.assertEqual(run.data["human"]["reason"], "premise invalidated")
        self.assertIsNotNone(run.data["human"]["since"])
        run.transition("operator_retry")
        self.assertIsNone(run.data["human"]["reason"])


class RetryAccountingTests(ModelTestCase):
    cfg = config.parse({})

    def test_scope_relaunches_are_free(self):
        run = self.new_run(SCOPING, "scope")
        for _ in range(3):
            self.attempt(run, "blocked")
        self.assertFalse(run.retry_needed("scope"))
        self.assertEqual(run.data["retries"]["used"], 0)

    def test_first_headless_attempt_is_free_and_later_ones_cost(self):
        run = self.new_run(QUEUED, "build")
        self.assertFalse(run.retry_needed())
        self.attempt(run, "blocked")
        self.assertTrue(run.retry_needed())

    def test_budget_is_shared_across_stages(self):
        run = self.new_run(RUNNING, "scope-review")
        attempt = self.attempt(run, "blocked")
        self.assertEqual(apply_decision(run, attempt, self.cfg), "retry")
        run.data["status"] = RUNNING
        attempt = self.attempt(run, "done", reason=None)
        self.assertEqual(apply_decision(run, attempt, self.cfg), "stage_passed")
        self.assertEqual(run.stage, "build")
        for _ in range(4):
            run.data["status"] = RUNNING
            attempt = self.attempt(run, "failed")
            self.assertEqual(apply_decision(run, attempt, self.cfg), "retry")
        self.assertEqual(run.data["retries"]["used"], 5)
        run.data["status"] = RUNNING
        attempt = self.attempt(run, "failed")
        self.assertEqual(apply_decision(run, attempt, self.cfg), "exhausted")
        self.assertEqual(run.status, CANCELLED)
        self.assertIn("retry budget exhausted (5/5)", run.data["human"]["reason"])

    def test_run_can_start_with_a_configured_retry_budget(self):
        run = Run.create(self.home / "runs", repo="/tmp/repo", request="Add a thing", plan="two-retries",
                         retry_budget=2)
        self.assertEqual(run.data["retries"], {"used": 0, "budget": 2})

    def test_lowered_config_budget_caps_an_existing_run_before_retrying(self):
        run = self.new_run(RUNNING, "build")
        run.data["retries"] = {"used": 2, "budget": 5}
        attempt = self.attempt(run, "failed")
        cfg = config.parse({"max_retries": 2})
        self.assertEqual(apply_decision(run, attempt, cfg), "exhausted")
        self.assertEqual(run.data["retries"], {"used": 2, "budget": 2})
        self.assertIn("retry budget exhausted (2/2)", run.data["human"]["reason"])

    def test_non_retryable_parks(self):
        run = self.new_run(RUNNING, "build")
        attempt = self.attempt(run, "blocked", reason="no spec", retryable=False)
        self.assertEqual(decide(run, attempt, stop_on_repeated_reason=False), ("park", "no spec"))

    def test_reported_usage_never_changes_the_decision(self):
        run = self.new_run(RUNNING, "build")
        attempt = self.attempt(run, "blocked", tokens=500)
        self.assertEqual(decide(run, attempt, stop_on_repeated_reason=False)[0], "retry")

    def test_repeated_reason_policy(self):
        run = self.new_run(RUNNING, "build")
        self.attempt(run, "blocked", reason="npm test failed in 12.3s")
        attempt = self.attempt(run, "blocked", reason="npm  test failed in 40s")
        self.assertEqual(decide(run, attempt, stop_on_repeated_reason=False)[0], "retry")
        action, reason = decide(run, attempt, stop_on_repeated_reason=True)
        self.assertEqual(action, "park")
        self.assertIn("same reason twice", reason)

    def test_different_reasons_keep_retrying(self):
        run = self.new_run(RUNNING, "build")
        self.attempt(run, "blocked", reason="lint failed")
        attempt = self.attempt(run, "blocked", reason="tests failed")
        self.assertEqual(decide(run, attempt, stop_on_repeated_reason=True)[0], "retry")

    def test_cancelled_attempt_cancels(self):
        run = self.new_run(RUNNING, "build")
        attempt = self.attempt(run, "cancelled", retryable=False)
        self.assertEqual(apply_decision(run, attempt, self.cfg), "cancel")
        self.assertEqual(run.status, CANCELLED)

    def test_ship_done_marks_run_done(self):
        run = self.new_run(RUNNING, "ship")
        attempt = self.attempt(run, "done", reason=None)
        self.assertEqual(apply_decision(run, attempt, self.cfg), "stage_passed")
        self.assertEqual(run.status, DONE)


class PersistenceTests(ModelTestCase):
    def test_run_ids_suffix_on_collision(self):
        first = Run.create(self.home / "runs", repo="/r", request="x", plan="same")
        second = Run.create(self.home / "runs", repo="/r", request="x", plan="same")
        self.assertNotEqual(first.id, second.id)
        self.assertTrue(second.id.endswith("-2"))

    def test_failed_replace_keeps_previous_state(self):
        run = self.new_run(QUEUED, "build")
        run.data["status"] = RUNNING
        with mock.patch("runner.model.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                run.save()
        self.assertEqual(Run.load(run.dir).status, QUEUED)
        self.assertEqual([p.name for p in run.dir.glob(".run.json.*")], [])

    def test_leftover_temp_file_does_not_affect_load(self):
        run = self.new_run(QUEUED, "build")
        (run.dir / ".run.json.partial").write_text("{truncated", encoding="utf-8")
        self.assertEqual(Run.load(run.dir).status, QUEUED)

    def test_newer_schema_is_rejected(self):
        run = self.new_run()
        data = json.loads((run.dir / "run.json").read_text())
        data["schema"] = 2
        (run.dir / "run.json").write_text(json.dumps(data))
        with self.assertRaisesRegex(SchemaError, "newer than this runner"):
            Run.load(run.dir)

    def test_invalid_json_is_a_schema_error(self):
        run = self.new_run()
        (run.dir / "run.json").write_text("{nope")
        with self.assertRaises(SchemaError):
            Run.load(run.dir)

    def test_timestamps_are_utc(self):
        run = self.new_run()
        self.assertTrue(run.data["created_at"].endswith("Z"))

    def test_events_are_appended_per_line(self):
        run = self.new_run()
        run.event("run.queued", "build")
        lines = (run.dir / "events.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(line)["event"] for line in lines], ["run.created", "run.queued"])
        with self.assertRaises(ValueError):
            run.event("not.an.event")


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = config.parse({})
        self.assertEqual(cfg.max_concurrent_stages, 4)
        self.assertEqual(cfg.max_retries, 5)
        self.assertEqual(cfg.sandbox_for("/any"), "workspace-write")

    def test_invalid_values(self):
        for bad in ({"max_concurrent_stages": 0}, {"max_retries": 0}, {"notify": "yes"}, {"schema": 2},
                    {"stage_poll_seconds": -1},
                    {"repos": {"relative/path": {}}}, {"repos": {"/abs": {"codex_sandbox": "none"}}}, []):
            with self.subTest(bad=bad), self.assertRaises(config.ConfigError):
                config.parse(bad)

    def test_unknown_keys_are_ignored_and_repo_keys_normalized(self):
        cfg = config.parse({"future": 1, "repos": {"/tmp/../tmp/x": {"codex_sandbox": "bypass"}}})
        self.assertEqual(cfg.sandbox_for("/tmp/x"), "bypass")
        self.assertEqual(cfg.raw["future"], 1)

    def test_invalid_json_file_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "config.json").write_text("{bad")
            with self.assertRaises(config.ConfigError):
                config.load(Path(tmp))
            self.assertEqual(Path(tmp, "config.json").read_text(), "{bad")


if __name__ == "__main__":
    unittest.main()
