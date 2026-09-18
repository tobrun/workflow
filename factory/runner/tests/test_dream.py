"""The dream loop: replay scoring, the selection and confirmation split, candidate checks, rounds, and deploys."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner import config, dream, records
from runner.tests.helpers import STUBS, FactoryTestCase

ROOT = Path(__file__).resolve().parents[3]


def decision(action: str, **extra) -> dict:
    body = {"schema": "factory.decision/1", "action": action, "summary": "s", "rationale": "r"}
    if action in ("launch", "repair", "regate", "advance"):
        body["stage"] = "build"
    if action == "launch":
        body["guidance"] = "g"
    if action == "repair":
        body["repair"] = {"instruction": "i"}
    if action == "park":
        body["park"] = {"reason": "r", "operator_action": "o"}
    if action in ("cancel", "rescope"):
        body["reason"] = "r"
    body.update(extra)
    return records.validate_decision(body)


class ScoreTests(unittest.TestCase):
    def test_an_accepted_action_one_ladder_step_above_the_cheapest_loses_the_step_penalty(self):
        self.assertEqual(dream.score_point(decision("launch"), {"accept": ["repair", "launch"]}), 0.85)

    def test_an_action_outside_the_accept_set_scores_nothing(self):
        self.assertEqual(dream.score_point(decision("park"), {"accept": ["regate"]}), 0)

    def test_an_override_the_label_does_not_allow_scores_nothing(self):
        advance = decision("advance", override={"gate_code": "result.missing", "justification": "j"})
        self.assertEqual(dream.score_point(advance, {"accept": ["advance"], "allow_override": False}), 0)
        self.assertEqual(dream.score_point(advance, {"accept": ["advance"], "allow_override": True}), 1)

    def test_a_malformed_decision_scores_nothing_and_keeps_its_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-message.md"
            path.write_text(json.dumps({"schema": "factory.decision/1", "action": "fly", "summary": "s",
                                        "rationale": "r"}))
            parsed, problem = dream.parse_decision(path)
        self.assertIsNone(parsed)
        self.assertIn("decision.action", problem)
        self.assertEqual(dream.score_point(parsed, {"accept": ["regate"]}), 0)

    def test_a_world_scores_the_mean_of_its_scorable_points(self):
        self.assertEqual(dream.score_world([{"score": 1, "scorable": True}, {"score": 0.85, "scorable": True},
                                            {"score": 0, "scorable": False}]), 0.925)
        self.assertIsNone(dream.score_world([{"score": 0, "scorable": False}]))

    def test_a_policy_scores_the_mean_over_worlds(self):
        self.assertEqual(dream.score_policy([0.925, 0.5]), 0.7125)
        self.assertEqual(dream.score_policy([0.925, None, 0.5]), 0.7125)

    def test_another_decision_schema_is_a_cache_miss(self):
        common = dict(policy="p", world="w", point="build-1", digest="d", model="m", effort="medium", repeat=1)
        self.assertNotEqual(dream.cache_key(schema="factory.decision/1", **common),
                            dream.cache_key(schema="factory.decision/2", **common))
        self.assertEqual(dream.cache_key(schema="factory.decision/1", **common),
                         dream.cache_key(schema="factory.decision/1", **common))


def point_dir(root: Path, name: str, *, plan: bool, files: tuple[str, ...] = ("gate.json",)) -> Path:
    """A world point on disk: a digest, an event naming its files, and optionally the plan."""
    point = root / name
    point.mkdir(parents=True)
    (point / "digest.json").write_text(json.dumps({"schema": "factory.digest/1", "generated_at": "2026-09-17T12:00:00Z",
                                                   "run": {"id": "r"}, "attempts": []}))
    named = "; ".join(f"{f.split('.')[0].replace('-', '_')} {f}" for f in files)
    (point / "event.md").write_text("Factory run r: event attempt.finished.\nbuild attempt 1 (stage) ended blocked.\n"
                                    f"Files: {named}; stderr <run_dir>/attempts/build-1/stderr.log\n")
    (point / "gate.json").write_text("{}")
    if plan:
        (point / "plan").mkdir()
        (point / "plan" / "spec.md").write_text("# Spec\n")
    return point


class ReplayTests(FactoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.replay_map = self.root / "replay.json"
        os.environ["FACTORY_STUB_REPLAY"] = str(self.replay_map)
        self.replay_map.write_text(json.dumps({"build-1": decision("regate")}))
        self.cfg = config.load(self.home)
        self.cache = dream.Cache(self.home / "dreams" / "cache")

    def replays(self) -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_ROLE") == "replay"]

    def test_a_first_pass_is_served_from_the_cache_the_second_time(self):
        point = point_dir(self.root / "world", "build-1", plan=True)
        first = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        second = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        self.assertEqual(first.decision["action"], "regate")
        self.assertEqual(second.decision, first.decision)
        self.assertEqual((first.fresh, second.fresh), (True, False))
        self.assertEqual(len(self.replays()), 1)
        call = self.replays()[0]
        self.assertEqual(call["env"]["FACTORY_REPLAY_POINT"], "build-1")
        self.assertEqual(Path(call["cwd"]).resolve(), point.resolve())
        self.assertIn("read-only", call["argv"])

    def test_a_repeat_is_a_fresh_draw(self):
        point = point_dir(self.root / "world", "build-1", plan=True)
        dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        again = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w", repeat=2)
        self.assertTrue(again.fresh)
        self.assertEqual(len(self.replays()), 2)
        # A later dream drawing the same repeat never gets the earlier draw back.
        self.assertTrue(dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w", repeat=2).fresh)
        self.assertEqual(len(self.replays()), 3)

    def test_the_prompt_discloses_what_a_planless_point_lacks(self):
        full = point_dir(self.root / "a", "build-1", plan=True)
        planless = point_dir(self.root / "b", "build-1", plan=False, files=("gate.json", "last-message.md"))
        dream.replay(full, dream.GENERATED, self.cfg, None, world="a")
        dream.replay(planless, dream.GENERATED, self.cfg, None, world="b")
        full_prompt, planless_prompt = (c["argv"][1] for c in self.replays())
        for prompt in (full_prompt, planless_prompt):
            self.assertIn("the worktree is not available, so decide", prompt)
        self.assertNotIn("plan files are not available", full_prompt)
        self.assertIn("plan files are not available", planless_prompt)
        self.assertIn("last_message last-message.md", planless_prompt)
        self.assertIn("stderr <run_dir>/attempts/build-1/stderr.log", planless_prompt)
        self.assertIn("stderr <run_dir>/attempts/build-1/stderr.log", full_prompt)
        self.assertIn(str(dream.GENERATED / "skills" / "foreman" / "SKILL.md"), full_prompt)


class EvalWrapperTests(FactoryTestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "factory" / "evals" / "foreman" / script), *args],
                              capture_output=True, text=True, env=dict(os.environ), timeout=120)

    def test_capturing_from_a_live_run_writes_the_case_and_defers_the_hand_label(self):
        run = self.queued_run(stage="build")
        attempt = run.begin_attempt("scope-review", host="codex", model="m", effort="low")
        run.finish_attempt(attempt, outcome="done", reason=None, retryable=False, source="gate")
        run.data["status"] = "running"
        run.save()
        out = self.root / "cases"
        result = self.run_script("capture.py", str(run.dir), "--stage", "scope-review", "--attempt", "1",
                                 "--name", "live-case", "--accept", "advance", "--out", str(out))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((out / "live-case" / "digest.json").is_file())
        self.assertTrue((out / "live-case" / "expected.json").is_file())
        self.assertIn("skipped, status running is not terminal", result.stdout)
        self.assertIn("factory history label --set", result.stdout)
        self.assertFalse((self.home / "history" / run.id).exists())
