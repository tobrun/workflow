"""The dream loop: replay scoring, the selection and confirmation split, candidate checks, rounds, and deploys."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from runner import config, dream, records
from runner.tests.helpers import STUBS, FactoryTestCase, git, make_factory_repo

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


class SplitTests(unittest.TestCase):
    def worlds(self, per_repo: dict[str, int]) -> list[dict]:
        return [{"id": f"20260917-{repo}{n:02d}-run", "repository": f"github.com/acme/{repo}"}
                for repo, count in per_repo.items() for n in range(count)]

    def test_three_repositories_hold_whole_repositories_back_for_confirmation(self):
        chosen = dream.split(self.worlds({"a": 3, "b": 3, "c": 3}))
        self.assertEqual(chosen.rule, "whole-repo")
        repo_of = {w["id"]: w["repository"] for w in self.worlds({"a": 3, "b": 3, "c": 3})}
        confirmation_repos = {repo_of[w] for w in chosen.confirmation}
        selection_repos = {repo_of[w] for w in chosen.selection}
        self.assertFalse(confirmation_repos & selection_repos)
        self.assertGreaterEqual(len(chosen.confirmation), 6)
        self.assertGreaterEqual(len(confirmation_repos), 2)

    def test_two_repositories_each_feed_both_sets(self):
        worlds = self.worlds({"a": 4, "b": 4})
        chosen = dream.split(worlds)
        repo_of = {w["id"]: w["repository"] for w in worlds}
        self.assertEqual(chosen.rule, "split-by-run")
        self.assertEqual({repo_of[w] for w in chosen.selection}, {"github.com/acme/a", "github.com/acme/b"})
        self.assertEqual({repo_of[w] for w in chosen.confirmation}, {"github.com/acme/a", "github.com/acme/b"})
        self.assertEqual(len(chosen.confirmation), 4)

    def test_one_repository_splits_by_run_and_says_the_repository_rule_could_not_apply(self):
        chosen = dream.split(self.worlds({"a": 4}))
        self.assertEqual(chosen.rule, "split-by-run")
        self.assertEqual((len(chosen.selection), len(chosen.confirmation)), (2, 2))
        self.assertIn("the repository rule could not apply", " ".join(chosen.notes))

    def test_input_order_never_changes_the_split(self):
        worlds = self.worlds({"a": 3, "b": 2, "c": 4})
        self.assertEqual(dream.split(worlds), dream.split(list(reversed(worlds))))
        self.assertEqual(dream.split(worlds).confirmation_hash, dream.split(worlds[::2] + worlds[1::2]).confirmation_hash)

    def test_a_single_world_leaves_confirmation_empty(self):
        chosen = dream.split(self.worlds({"a": 1}))
        self.assertEqual((chosen.rule, chosen.confirmation), ("too-small", []))
        self.assertEqual(len(chosen.confirmation_hash), 64)


INCUMBENT = (ROOT / "factory" / "skills" / "foreman" / "SKILL.md").read_text(encoding="utf-8")


class CandidateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.deny = dream.Denylist({"20260917-1200-fixture", "github.com/acme/app"},
                                   dream._windows("the playwright config pins port 4173 but vite serves on 5173"))

    def check(self, text: str) -> str | None:
        failed = dream.check_candidate(text, self.deny, INCUMBENT)
        return failed[0] if failed else None

    def with_line(self, line: str) -> str:
        return INCUMBENT.rstrip("\n") + "\n" + line + "\n"

    def test_the_incumbent_passes_its_own_checks(self):
        self.assertIsNone(self.check(INCUMBENT))

    def test_a_forty_character_run_of_a_recorded_gate_reason_is_rejected(self):
        self.assertEqual(self.check(self.with_line("Remember: the Playwright config pins port 4173 but Vite serves.")),
                         "shingle")

    def test_an_aws_key_shaped_literal_is_rejected(self):
        self.assertEqual(self.check(self.with_line("Never print AKIA" + "IOSFODNN7EXAMPLE in guidance.")), "secret")

    def test_the_short_slack_token_from_the_recorded_condition_is_rejected(self):
        self.assertEqual(self.check(self.with_line("A token like xoxb-" + "1234567890-abcdefghijklmn is a secret.")),
                         "secret")

    def test_a_selection_run_id_is_rejected(self):
        self.assertEqual(self.check(self.with_line("As run 20260917-1200-fixture showed, regate first.")), "denylist")

    def test_a_skill_past_the_line_limit_is_rejected(self):
        lines = INCUMBENT.count("\n")
        self.assertEqual(self.check(INCUMBENT + "Keep it short.\n" * (151 - lines)), "length")

    def test_a_decision_table_without_rescope_is_rejected(self):
        text = "\n".join(line for line in INCUMBENT.splitlines() if not line.startswith("| `rescope`")) + "\n"
        self.assertEqual(self.check(text), "actions")

    def test_a_sentence_routing_the_decision_to_a_person_is_rejected(self):
        self.assertEqual(self.check(self.with_line("When unsure, ask the operator which stage to launch.")), "F03")


def make_world(home: Path, run_id: str, repository: str, points: list[str], *, unscorable: tuple = (),
               labelled: bool = True, accept: tuple = ("advance",), reason: str = "the gate passed") -> Path:
    """A world written straight to disk: points with a digest and an event, and human labels."""
    from runner import history
    world_dir = home / "history" / run_id
    entries = []
    for point in points:
        stage, n = point.rsplit("-", 1)
        pdir = world_dir / "points" / point
        pdir.mkdir(parents=True)
        (pdir / "digest.json").write_text(json.dumps({"schema": "factory.digest/1", "generated_at": "2026-09-17T12:00:00Z",
                                                      "run": {"id": run_id, "plan": "fixture"}, "attempts": []}))
        (pdir / "event.md").write_text(f"Factory run {run_id}: event attempt.finished.\n{stage} attempt {n} ended done.\n"
                                       "Files: gate gate.json\n")
        (pdir / "gate.json").write_text(json.dumps({"passed": True, "reason": reason}))
        entries.append({"id": point, "stage": stage, "attempt": int(n), "scorable": point not in unscorable,
                        "snapshot": "recorded", "plan": False, "origin": "cold", "decision_source": "foreman",
                        "event": {"kind": "attempt.finished", "outcome": "done", "reason": reason}})
    world = {"schema": "factory.world/1", "run": {"id": run_id, "plan": "fixture", "attempts": []},
             "repository": repository, "terminal_status": "done", "decision_schema": "factory.decision/1",
             "source_sha256": "0" * 64, "builder": "test", "points": entries}
    (world_dir / "world.json").write_text(json.dumps(records.validate_world(world)))
    if labelled:
        # The labeller writes labels.json even when no point is scorable; it then holds no entries.
        (world_dir / "labels.json").write_text(json.dumps({"schema": "factory.labels/1", "run": run_id, "labels": {}}))
        for point in points:
            if point not in unscorable:
                history.set_label(world_dir, point, list(accept))
    return world_dir


class RoundTestCase(FactoryTestCase):
    """Dream rounds against a throwaway copy of this repository, so no test can deploy into the real checkout."""

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls._repo_tmp = tempfile.TemporaryDirectory(prefix="factory-repo-")
        cls.factory_repo = make_factory_repo(Path(os.path.realpath(cls._repo_tmp.name)) / "workflow")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._repo_tmp.cleanup()
        super().tearDownClass()

    def setUp(self) -> None:
        super().setUp()
        os.environ["FACTORY_REPO_ROOT"] = str(self.factory_repo)
        self.files = {name: self.root / f"{name}.json" for name in ("replay", "candidate", "dream", "hindsight")}
        os.environ.update({"FACTORY_STUB_REPLAY": str(self.files["replay"]),
                           "FACTORY_STUB_REPLAY_CANDIDATE": str(self.files["candidate"]),
                           "FACTORY_STUB_DREAM": str(self.files["dream"]),
                           "FACTORY_STUB_HINDSIGHT": str(self.files["hindsight"])})
        self.answers(incumbent=decision("park"), candidate=decision("advance"))

    def answers(self, *, incumbent: dict, candidate: dict) -> None:
        self.files["replay"].write_text(json.dumps({"*": incumbent}))
        self.files["candidate"].write_text(json.dumps({"*": candidate}))

    def dream_step(self, step: object) -> None:
        self.files["dream"].write_text(json.dumps({"1": step}))

    def call(self, *argv: str) -> tuple[int, str, str]:
        import contextlib
        import io
        from runner import cli
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def replays(self, *, candidate: bool | None = None) -> list[dict]:
        calls = [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_ROLE") == "replay"]
        if candidate is None:
            return calls
        return [c for c in calls if ("/candidates/" in c["argv"][1].split("\n", 1)[0]) == candidate]

    def dream_dir(self) -> Path:
        dirs = sorted(p for p in (self.home / "dreams").iterdir() if p.name != "cache")
        return dirs[-1]

    def decision(self) -> dict:
        return json.loads((self.dream_dir() / "decision.json").read_text())

    def report(self) -> str:
        return (self.dream_dir() / "report.md").read_text()


class RoundTests(RoundTestCase):
    def test_a_candidate_that_scores_higher_on_selection_is_named_best(self):
        world = make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1", "ship-1"])
        (world / "faults.json").write_text(json.dumps({"schema": "factory.faults/1", "run": world.name, "faults": [
            {"kind": "harness", "code_family": "record.invalid", "summary": "s", "evidence": [], "attempts": 2,
             "tokens": 900}]}))
        code, out, _ = self.call("dream", "--rounds", "1")
        self.assertEqual(code, 0, out)
        decision_ = self.decision()
        self.assertEqual(decision_["best"], "round-1")
        self.assertEqual(decision_["selection"], {"incumbent": 0.0, "round-1": 1.0})
        self.assertIn("Best on selection: round-1", self.report())
        self.assertIn("| **policy** | 0.000 | 1.000 |", self.report())
        self.assertIn("| harness | record.invalid | 1 | 2 | 900 | 1 |", self.report())
        incumbent_points = sorted(Path(c["cwd"]).name for c in self.replays(candidate=False))
        candidate_points = sorted(Path(c["cwd"]).name for c in self.replays(candidate=True))
        self.assertEqual(incumbent_points, ["build-1", "ship-1"])
        self.assertEqual(candidate_points, incumbent_points)
        staged = self.dream_dir() / "candidates" / "1" / "plugins" / "factory"
        self.assertTrue((staged / "references" / "factory-run.md").is_file())
        skill = (staged / "skills" / "foreman" / "SKILL.md").read_text()
        self.assertIn("Read the gate reason before deciding.", skill)
        self.assertNotIn("disable-model-invocation", skill)

    def test_a_candidate_that_scores_lower_leaves_the_incumbent_best(self):
        self.answers(incumbent=decision("advance"), candidate=decision("park"))
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        self.assertEqual(self.call("dream", "--rounds", "1")[0], 0)
        decision_ = self.decision()
        self.assertEqual(decision_["best"], "incumbent")
        self.assertEqual(decision_["selection"], {"incumbent": 1.0, "round-1": 0.0})
        self.assertFalse(decision_["deployed"])
        self.assertNotIn("commit", decision_)
        self.assertIn("Best on selection: incumbent", self.report())

    def test_the_call_cap_ends_the_round_partial_without_a_deploy(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1", "build-2", "ship-1"])
        code, out, _ = self.call("dream", "--rounds", "1", "--max-calls", "2")
        self.assertEqual(code, 0, out)
        decision_ = self.decision()
        self.assertTrue(decision_["partial"])
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "partial"))
        self.assertEqual(len(self.replays()), 2)
        self.assertIn("partial: the call cap was reached, so nothing deploys", self.report())

    def test_a_second_dream_while_the_history_lock_is_held_does_no_work(self):
        from runner import history
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        with history.HistoryLock(self.home):
            code, out, err = self.call("dream", "--rounds", "1")
        self.assertEqual(code, 3)
        self.assertIn("history.lock", err)
        self.assertFalse((self.home / "dreams").exists())
        self.assertEqual(self.replays(), [])

    def test_zero_repeats_is_refused_before_any_paid_call(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        code, _, err = self.call("dream", "--repeats", "0")
        self.assertEqual(code, 2)
        self.assertIn("--repeats must be at least 1", err)
        self.assertEqual(self.stub_calls("codex"), [])

    def test_an_unknown_world_is_refused_with_the_known_ids(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        code, _, err = self.call("dream", "--worlds", "20990101-0000-nope")
        self.assertEqual(code, 2)
        self.assertIn("unknown world(s) 20990101-0000-nope; known: 20260917-1200-a", err)
        self.assertEqual(self.stub_calls("codex"), [])

    def test_confirmation_is_scored_for_both_policies_and_its_set_is_tracked(self):
        for n in range(4):
            make_world(self.home, f"20260917-120{n}-a", "github.com/acme/app", ["build-1"])
        self.assertEqual(self.call("dream", "--rounds", "1")[0], 0)
        decision_ = self.decision()
        confirmation = decision_["split"]["confirmation"]
        self.assertEqual(confirmation, ["20260917-1201-a", "20260917-1203-a"])
        confirmed_by = {(Path(c["cwd"]).parent.parent.name, "/candidates/" in c["argv"][1].split("\n", 1)[0])
                        for c in self.replays()}
        for world in confirmation:
            self.assertIn((world, False), confirmed_by)
            self.assertIn((world, True), confirmed_by)
        self.assertEqual(decision_["confirmation"], {"incumbent": 0.0, "candidate": 1.0})
        self.assertEqual(decision_["reason"], "floor")
        self.assertEqual(len(decision_["split"]["confirmation_hash"]), 64)
        self.assertIn("Applied rule: split-by-run", self.report())
        self.assertIn("has served 0 earlier dream(s)", self.report())
        self.assertIn("| round-1 | 1.000 |", self.report())
        self.assertEqual(self.call("dream", "--rounds", "1")[0], 0)
        self.assertIn("has served 1 earlier dream(s)", self.report())

    def test_one_world_leaves_confirmation_empty_and_the_floor_refuses(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        code, out, _ = self.call("dream", "--rounds", "1")
        self.assertEqual(code, 0)
        self.assertIn("decision floor", out)
        self.assertEqual(self.decision()["reason"], "floor")
        self.assertEqual(self.decision()["split"]["confirmation"], [])
        self.assertIn("| (none) | confirmation was not scored |", self.report())

    def test_unlabelled_worlds_are_excluded_and_named(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        make_world(self.home, "20260917-1201-a", "github.com/acme/app", ["build-1"])
        make_world(self.home, "20260917-1202-a", "github.com/acme/app", ["build-1"], labelled=False)
        self.answers(incumbent=decision("advance"), candidate=decision("advance"))
        self.assertEqual(self.call("dream", "--rounds", "1", "--no-deploy")[0], 0)
        self.assertEqual(self.decision()["selection"]["incumbent"], 1.0)
        self.assertEqual(self.decision()["split"]["selection"] + self.decision()["split"]["confirmation"],
                         ["20260917-1200-a", "20260917-1201-a"])
        self.assertIn("excluded: 20260917-1202-a has no labels", self.report())
        self.assertFalse([c for c in self.replays() if "20260917-1202-a" in c["cwd"]])

    def test_a_candidate_naming_a_selection_run_is_rejected_before_any_replay(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        self.dream_step({"append": "As run 20260917-1200-a showed, regate first."})
        self.assertEqual(self.call("dream", "--rounds", "1")[0], 0)
        self.assertEqual(self.replays(candidate=True), [])
        self.assertEqual(len(self.replays(candidate=False)), 1)
        self.assertIn("Rejected by `denylist`", self.report())
        self.assertEqual(self.decision()["best"], "incumbent")

    def test_a_world_without_a_scorable_point_is_never_replayed(self):
        from runner import history
        unscored = make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"], unscorable=("build-1",))
        # Even a hand label never makes a hard-stop point scorable.
        history.set_label(unscored, "build-1", ["park"])
        make_world(self.home, "20260917-1201-a", "github.com/acme/app", ["build-1"])
        make_world(self.home, "20260917-1202-a", "github.com/acme/app", ["build-1"])
        self.assertEqual(self.call("dream", "--rounds", "1", "--no-deploy")[0], 0)
        self.assertEqual(self.decision()["split"]["selection"], ["20260917-1200-a", "20260917-1202-a"])
        self.assertFalse([c for c in self.replays() if "20260917-1200-a" in c["cwd"]])
        self.assertEqual(self.decision()["selection"]["incumbent"], 0.0)
        self.assertIn("excluded: 20260917-1200-a has no scorable labelled point", self.report())


class ReadOnlyTests(RoundTestCase):
    def test_labelling_and_dreaming_never_write_inside_a_run_directory(self):
        from runner.tests.test_history import tree_hashes
        from runner.worker import Worker
        self.configure(foreman="codex")
        self.foreman([{"action": "advance", "stage": s} for s in ("scope-review", "build", "ship")])
        run = self.queued_run()
        self.assertEqual(Worker(self.home, run.id, grace=1).run(), 0)
        self.assertEqual(self.call("history", "build", "--all")[0], 0)
        before = tree_hashes(run.dir)
        self.files["hindsight"].write_text(json.dumps("auto"))
        self.assertEqual(self.call("history", "label", "--all")[0], 0)
        self.assertEqual(self.call("dream", "--rounds", "1", "--no-deploy")[0], 0)
        self.assertTrue(self.replays())
        self.assertEqual(tree_hashes(run.dir), before)


class Score:
    def __init__(self, mean: float | None, name: str = "round-1"):
        self.mean, self.name = mean, name


def confirmation_views(worlds: int, points: int, repositories: list[str]) -> list:
    views = []
    for n in range(worlds):
        ids = [f"build-{i + 1}" for i in range(points)]
        world = {"repository": repositories[n % len(repositories)],
                 "points": [{"id": p, "scorable": True} for p in ids]}
        views.append(dream.WorldView(f"20260917-12{n:02d}-c", Path("/nonexistent"), world,
                                     {p: {"accept": ["advance"]} for p in ids}))
    return views


CANDIDATE_LINE = "Read the gate reason before deciding."


class DeployTests(FactoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.repo = make_factory_repo(self.root / "workflow")
        (self.repo / ".dev" / "plan").mkdir(parents=True)
        (self.repo / ".dev" / "plan" / "spec.md").write_text("untracked plan\n")
        self.cfg = config.parse({})
        self.source = self.repo / "factory" / "skills" / "foreman" / "SKILL.md"
        self.generated = self.repo / "plugins" / "factory" / "skills" / "foreman" / "SKILL.md"
        self.originals = (self.source.read_bytes(), self.generated.read_bytes())
        self.dream_dir = self.home / "dreams" / "20260918-1015-dream"
        self.dream_dir.mkdir(parents=True)
        text = self.source.read_text().rstrip("\n") + f"\n{CANDIDATE_LINE}\n"
        tree = dream.stage_plugin(text, self.dream_dir / "candidates" / "1" / "plugins" / "factory",
                                  self.repo / "plugins" / "factory")
        self.candidate = {"text": text, "tree": tree, "name": "round-1"}

    def deploy(self, *, gain: float = 0.06, worlds: int = 6, points: int = 5,
               repositories: tuple = ("github.com/acme/app", "github.com/acme/web"), partial: bool = False) -> dict:
        views = confirmation_views(worlds, points, list(repositories)) if worlds else []
        scores = {"incumbent": Score(0.5, "incumbent"), "candidate": Score(0.5 + gain)}
        return dream.deploy(dream_dir=self.dream_dir, candidate=self.candidate, confirmation=scores,
                            confirmation_worlds=views, partial=partial, cfg=self.cfg, root=self.repo, echo=lambda _: None)

    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD")

    def assert_untouched(self, head: str) -> None:
        self.assertEqual(self.head(), head)
        self.assertEqual((self.source.read_bytes(), self.generated.read_bytes()), self.originals)
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_winner_over_the_floor_and_the_margin_is_committed_and_pushed(self):
        before = self.head()
        decision_ = self.deploy(gain=0.06)
        self.assertEqual((decision_["deployed"], decision_["reason"]), (True, "deployed"), decision_)
        commit = self.head()
        self.assertNotEqual(commit, before)
        changed = git(self.repo, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).splitlines()
        self.assertEqual(sorted(changed), ["factory/skills/foreman/SKILL.md", "plugins/factory/skills/foreman/SKILL.md"])
        self.assertEqual(git(self.repo, "rev-parse", "origin/main"), commit)
        self.assertEqual(git(self.root / "workflow-origin.git", "rev-parse", "main"), commit)
        self.assertIn(CANDIDATE_LINE, self.generated.read_text())
        self.assertEqual(len(decision_["source_hash"]), 64)
        self.assertEqual(decision_["generated_hash"], dream.provenance.tree_policy(self.repo / "plugins" / "factory")["sha256"])
        message = git(self.repo, "log", "-1", "--format=%B")
        self.assertIn("feat(foreman): dream 20260918-1015-dream raises confirmation score 0.500 -> 0.560", message)
        self.assertIn(decision_["generated_hash"], message)
        self.assertTrue((self.repo / ".dev" / "plan" / "spec.md").is_file())

    def test_a_gain_under_the_margin_is_refused(self):
        head = self.head()
        self.assertEqual(self.deploy(gain=0.04)["reason"], "margin")
        self.assert_untouched(head)

    def test_the_margin_is_at_least_one_confirmation_point(self):
        head = self.head()
        decision_ = self.deploy(gain=0.08, worlds=6, points=1, repositories=("a", "b"))
        self.assertEqual(decision_["reason"], "margin")
        self.assertIn("needs a gain of 0.167", decision_["cause"])
        decision_ = self.deploy(gain=0.08, worlds=10, points=1)
        self.assertEqual(decision_["reason"], "margin")
        self.assertIn("needs a gain of 0.100", decision_["cause"])
        self.assert_untouched(head)

    def test_one_remote_under_many_checkouts_is_one_repository_below_the_floor(self):
        from runner import history
        remotes = ["git@github.com:acme/app.git", "https://github.com/acme/app", "https://github.com/acme/app.git",
                   "ssh://git@github.com/acme/app.git", "git@github.com:Acme/App.git"]
        head = self.head()
        decision_ = self.deploy(repositories=tuple(history.repo_key(r) for r in remotes))
        self.assertEqual(decision_["reason"], "floor")
        self.assertIn("from 1 repository", decision_["cause"])
        self.assert_untouched(head)

    def test_five_confirmation_worlds_are_below_the_floor(self):
        head = self.head()
        self.assertEqual(self.deploy(worlds=5)["reason"], "floor")
        self.assert_untouched(head)

    def test_no_confirmation_or_nothing_scorable_in_it_is_below_the_floor(self):
        head = self.head()
        self.assertEqual(self.deploy(worlds=0)["reason"], "floor")
        self.assertEqual(self.deploy(points=0)["reason"], "floor")
        self.assert_untouched(head)

    def test_a_partial_round_never_deploys(self):
        head = self.head()
        self.assertEqual(self.deploy(partial=True)["reason"], "partial")
        self.assert_untouched(head)

    def test_a_failing_validate_restores_both_skill_files(self):
        os.environ["FACTORY_TEST_VALIDATE_EXIT"] = "1"
        head = self.head()
        self.assertEqual(self.deploy()["reason"], "validate")
        self.assert_untouched(head)

    def test_a_failing_plugin_build_restores_both_skill_files(self):
        head = self.head()
        plugins = self.repo / "plugins"
        plugins.chmod(0o555)
        try:
            self.assertEqual(self.deploy()["reason"], "build_failed")
        finally:
            plugins.chmod(0o755)
        self.assert_untouched(head)

    def test_a_tracked_change_refuses_the_deploy(self):
        (self.repo / "factory" / "references" / "factory-run.md").write_text("edited\n")
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual(decision_["reason"], "dirty")
        self.assertIn("factory/references/factory-run.md", decision_["cause"])
        self.assertIn("dirty", __import__("runner.cli", fromlist=["cli"]).DEPLOY_FAILURES)
        self.assertEqual(self.head(), head)

    def test_a_stale_generated_tree_refuses_before_writing_the_skill(self):
        (self.repo / "factory" / "references" / "factory-run.md").write_text("edited, never rebuilt\n")
        git(self.repo, "commit", "--quiet", "-am", "edit the protocol without rebuilding")
        head = self.head()
        self.assertEqual(self.deploy()["reason"], "stale_plugins")
        self.assert_untouched(head)

    def test_a_commit_without_an_identity_restores_both_skill_files(self):
        git(self.repo, "config", "--unset", "user.name")
        git(self.repo, "config", "--unset", "user.email")
        git(self.repo, "config", "user.useConfigOnly", "true")
        (self.root / "empty-gitconfig").write_text("")
        os.environ.update({"GIT_CONFIG_GLOBAL": str(self.root / "empty-gitconfig"),
                           "GIT_CONFIG_SYSTEM": str(self.root / "empty-gitconfig")})
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
            os.environ.pop(name, None)
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual(decision_["reason"], "commit_failed", decision_)
        self.assertIn("commit_failed", __import__("runner.cli", fromlist=["cli"]).DEPLOY_FAILURES)
        self.assert_untouched(head)

    def test_a_push_to_a_vanished_origin_keeps_the_commit(self):
        import shutil
        shutil.rmtree(self.root / "workflow-origin.git")
        before = self.head()
        decision_ = self.deploy()
        self.assertEqual(decision_["reason"], "push_failed")
        self.assertEqual(decision_["cause"], "network")
        self.assertNotEqual(self.head(), before)
        self.assertEqual(decision_["commit"], self.head())
        self.assertIn("push_failed", __import__("runner.cli", fromlist=["cli"]).DEPLOY_FAILURES)

    def test_an_unpushed_deploy_is_pushed_again_before_any_new_one(self):
        other = self.root / "other"
        git(self.root, "clone", "--quiet", str(self.root / "workflow-origin.git"), str(other))
        (other / "NOTES.md").write_text("someone else pushed first\n")
        git(other, "add", "NOTES.md")
        git(other, "commit", "--quiet", "-m", "docs: notes")
        git(other, "push", "--quiet", "origin", "main")
        first = self.deploy()
        self.assertEqual((first["reason"], first["cause"]), ("push_failed", "non-fast-forward"))
        head = self.head()
        second = self.deploy()
        self.assertEqual((second["reason"], second["cause"]), ("push_failed", "non-fast-forward"))
        self.assertEqual(second["pending"], [head])
        self.assertEqual(self.head(), head)

    def test_a_deploy_never_writes_inside_a_run_directory(self):
        from runner.tests.test_history import tree_hashes
        run = self.queued_run()
        before = tree_hashes(run.dir)
        self.assertTrue(self.deploy()["deployed"])
        self.assertEqual(tree_hashes(run.dir), before)


class FullRoundDeployTests(RoundTestCase):
    def test_a_winning_round_lands_on_the_fixture_main_with_its_policy_hash(self):
        repo = make_factory_repo(self.root / "workflow")
        os.environ["FACTORY_REPO_ROOT"] = str(repo)
        self.configure(dream_floor_worlds=2, dream_floor_repos=1)
        for n in range(4):
            make_world(self.home, f"20260917-120{n}-a", "github.com/acme/app", ["build-1", "ship-1"])
        code, out, _ = self.call("dream", "--rounds", "1")
        self.assertEqual(code, 0, out)
        decision_ = self.decision()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (True, "deployed"), decision_)
        self.assertIn("codex plugin add factory@nurbot", out)
        head = git(repo, "rev-parse", "HEAD")
        self.assertEqual(decision_["commit"], head)
        self.assertEqual(git(self.root / "workflow-origin.git", "rev-parse", "main"), head)
        committed = hashlib.sha256()
        for path in ("plugins/factory/skills/foreman/SKILL.md", "plugins/factory/references/factory-run.md"):
            content = subprocess.run(["git", "-C", str(repo), "show", f"HEAD:{path}"], capture_output=True,
                                     check=True).stdout
            committed.update(hashlib.sha256(content).hexdigest().encode("ascii") + b"\n")
        self.assertEqual(decision_["generated_hash"], committed.hexdigest())
