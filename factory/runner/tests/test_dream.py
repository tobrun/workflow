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
from unittest import mock

from runner import config, dream, records
from runner.tests.helpers import FactoryTestCase, call_cli, git, make_factory_repo

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
        common = {"policy": "p", "world": "w", "point": "build-1", "digest": "d", "model": "m", "effort": "medium",
                  "repeat": 1}
        self.assertNotEqual(dream.cache_key(schema="factory.decision/1", **common),
                            dream.cache_key(schema="factory.decision/2", **common))
        self.assertEqual(dream.cache_key(schema="factory.decision/1", **common),
                         dream.cache_key(schema="factory.decision/1", **common))

    def test_scores_are_rounded_to_six_decimals(self):
        with mock.patch.object(dream, "STEP_PENALTY", 1 / 3):
            self.assertEqual(dream.score_point(decision("launch"), {"accept": ["repair", "launch"]}), 0.666667)
        self.assertEqual(dream.score_world([{"score": 1}, {"score": 0}, {"score": 0}]), 0.333333)
        self.assertEqual(dream.score_policy([1.0, 0.0, 0.0]), 0.333333)

    def test_a_point_without_a_scorable_flag_counts(self):
        self.assertEqual(dream.score_world([{"score": 0.5}]), 0.5)

    def test_a_missing_final_message_is_a_problem_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(dream.parse_decision(Path(tmp) / "last-message.md"),
                             (None, "the replay wrote no final message"))

    def test_a_decision_surrounded_by_prose_is_parsed(self):
        body = json.dumps(decision("regate"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-message.md"
            for text in (f"My decision:\n{body}. That is all.", f"{body}. That is all."):
                path.write_text(text)
                self.assertEqual(dream.parse_decision(path), (decision("regate"), None))

    def test_a_message_without_an_opening_brace_is_parsed_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "last-message.md"
            path.write_text('"}"')
            parsed, problem = dream.parse_decision(path)
        self.assertIsNone(parsed)
        self.assertTrue(problem.startswith("RecordError: "), problem)


class MissingEventFilesTests(unittest.TestCase):
    def test_only_files_lines_are_read_and_only_named_paths_that_are_absent_are_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            point = Path(tmp)
            (point / "gate.json").write_text("{}")
            (point / "event.md").write_text("Factory run r: event attempt.finished.\n"
                                            "Files: gate gate.json; bare; stderr <run_dir>/stderr.log\n")
            self.assertEqual(dream.missing_event_files(point), ["stderr <run_dir>/stderr.log"])


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

    def test_a_replay_that_exits_non_zero_is_reported_and_never_cached(self):
        point = point_dir(self.root / "world", "build-1", plan=True)
        self.replay_map.write_text("not json")
        failed = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        self.assertEqual((failed.decision, failed.fresh), (None, True))
        self.assertRegex(failed.problem, r"^replay exited [1-9]")
        self.replay_map.write_text(json.dumps({"build-1": decision("regate")}))
        again = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        self.assertEqual((again.decision["action"], again.fresh), ("regate", True))

    def test_a_replay_host_failure_is_retried_once_and_then_marked_failed_not_answered(self):
        point = point_dir(self.root / "world", "build-1", plan=True)
        self.replay_map.write_text(json.dumps({"build-1": {"mode": "exit", "exit": 1}}))
        budget = dream.Budget(10)
        failed = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w", budget=budget)
        self.assertEqual(len(self.replays()), 2)
        self.assertEqual(budget.spent, 2)
        self.assertEqual((failed.decision, failed.failed, failed.skipped), (None, True, False))
        self.assertEqual(failed.problem, "replay exited 1")
        self.assertIsNone(self.cache.get(failed.key))

    def test_a_replay_whose_host_cannot_start_did_not_finish(self):
        os.environ["FACTORY_CODEX_BIN"] = str(self.root / "no-such-codex")
        point = point_dir(self.root / "world", "build-1", plan=True)
        answer = dream.replay(point, dream.GENERATED, self.cfg, self.cache, world="w")
        self.assertIsNone(answer.decision)
        self.assertIn("replay did not finish", answer.problem)
        self.assertIsNone(self.cache.get(answer.key))


class EvalWrapperTests(FactoryTestCase):
    def run_script(self, script: str, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(ROOT / "factory" / "evals" / "foreman" / script), *args],
                              capture_output=True, text=True, env=dict(os.environ), timeout=120, check=False)

    def test_the_eval_runner_passes_a_case_the_replay_stub_answers(self):
        answers = self.root / "replay.json"
        answers.write_text(json.dumps({"two-ideas-ship-4": {"action": "publish", "summary": "push the ship commits"}}))
        os.environ["FACTORY_STUB_REPLAY"] = str(answers)
        result = self.run_script("run.py", "--case", "two-ideas-ship-4", "--out", str(self.root / "eval"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("PASS  two-ideas-ship-4: publish - push the ship commits", result.stdout)
        answers.write_text(json.dumps({"two-ideas-ship-4": {"action": "park", "park": {
            "reason": "r", "operator_action": "o"}}}))
        result = self.run_script("run.py", "--case", "two-ideas-ship-4", "--out", str(self.root / "eval-2"))
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  two-ideas-ship-4: park", result.stdout)

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

    def test_whole_repositories_stop_once_both_confirmation_targets_are_met(self):
        chosen = dream.split(self.worlds({"a": 3, "b": 3, "c": 3, "d": 3}))
        self.assertEqual(chosen.rule, "whole-repo")
        self.assertEqual({w.split("-")[1][0] for w in chosen.confirmation}, {"a", "b"})
        self.assertEqual(len(chosen.selection), 6)
        self.assertIn("github.com/acme/a, github.com/acme/b", chosen.notes[0])

    def test_a_repository_with_one_world_splits_by_run_and_is_named(self):
        chosen = dream.split(self.worlds({"a": 3, "b": 1}))
        self.assertEqual(chosen.rule, "split-by-run")
        self.assertEqual(chosen.confirmation, ["20260917-a01-run", "20260917-b00-run"])
        self.assertEqual(chosen.notes, [("repository github.com/acme/b holds fewer than two worlds and could not span "
                                         "both sets")])

    def test_input_order_never_changes_the_split(self):
        worlds = self.worlds({"a": 3, "b": 2, "c": 4})
        self.assertEqual(dream.split(worlds), dream.split(list(reversed(worlds))))
        self.assertEqual(dream.split(worlds).confirmation_hash, dream.split(worlds[::2] + worlds[1::2]).confirmation_hash)

    def test_a_single_world_leaves_confirmation_empty(self):
        chosen = dream.split(self.worlds({"a": 1}))
        self.assertEqual((chosen.rule, chosen.confirmation), ("too-small", []))
        self.assertEqual(len(chosen.confirmation_hash), 64)

    def test_two_worlds_are_enough_to_hold_one_back(self):
        chosen = dream.split(self.worlds({"a": 2}))
        self.assertEqual((chosen.rule, chosen.selection, chosen.confirmation),
                         ("split-by-run", ["20260917-a00-run"], ["20260917-a01-run"]))

    def test_whole_repositories_continue_until_the_repository_target_is_met_too(self):
        chosen = dream.split(self.worlds({"a": 6, "b": 1, "c": 1, "d": 1}))
        self.assertEqual(chosen.rule, "whole-repo")
        self.assertEqual({w.split("-")[1][0] for w in chosen.confirmation}, {"a", "b"})
        self.assertEqual(len(chosen.confirmation), 7)

    def test_two_repositories_of_two_or_more_worlds_split_each_repository_by_run(self):
        chosen = dream.split(self.worlds({"a": 3, "b": 2}))
        self.assertEqual(chosen.rule, "split-by-run")
        self.assertEqual(chosen.confirmation, ["20260917-a01-run", "20260917-b01-run"])
        self.assertEqual(chosen.notes, ["two repositories: each one feeds both sets, split by run"])


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

    def test_a_skill_without_the_incumbent_frontmatter_is_rejected(self):
        self.assertEqual(self.check("Preface.\n" + INCUMBENT), "frontmatter")

    def test_an_em_dash_is_rejected(self):
        self.assertEqual(self.check(self.with_line("Regate first \u2014 then advance.")), "emdash")

    def test_any_run_id_shape_is_rejected_even_from_an_unseen_run(self):
        self.assertEqual(self.check(self.with_line("As run 20250101-0900-other showed, regate first.")), "denylist")

    def filled(self, lines: int, *, newline: bool) -> str:
        text = INCUMBENT + "Keep it short.\n" * (lines - INCUMBENT.count("\n"))
        return text if newline else text.rstrip("\n")

    def test_exactly_the_line_limit_passes_with_or_without_a_final_newline(self):
        self.assertIsNone(self.check(self.filled(150, newline=True)))
        self.assertIsNone(self.check(self.filled(150, newline=False)))

    def test_one_line_past_the_limit_without_a_final_newline_is_rejected(self):
        self.assertEqual(dream.check_candidate(self.filled(151, newline=False), self.deny, INCUMBENT),
                         ("length", "151 lines, the limit is 150"))

    def test_a_changed_frontmatter_is_rejected(self):
        text = INCUMBENT.replace("name: foreman\n", "name: foreman-two\n", 1)
        self.assertEqual(self.check(text), "frontmatter")

    def test_the_f03_detail_quotes_the_whole_phrase(self):
        failed = dream.check_candidate(self.with_line("When unsure, ask the operator which stage to launch."),
                                       self.deny, INCUMBENT)
        self.assertEqual(failed, ("F03", "routes a decision to a person: 'ask the operator'"))

    def test_a_github_token_of_the_minimum_length_is_rejected_with_the_truncated_pattern(self):
        token = "ghp_" + "a" * 30
        failed = dream.check_candidate(self.with_line(f"Never print {token} in guidance."), self.deny, INCUMBENT)
        self.assertEqual(failed, ("secret", "a credential-shaped literal matching \\b(?:gh[pousr]_[0-9A-Za-z]{30,"))
        self.assertIsNone(self.check(self.with_line(f"Never print {token[:-1]} in guidance.")))


class WindowTests(unittest.TestCase):
    def test_a_window_is_exactly_forty_normalized_characters(self):
        text = "".join(chr(ord("a") + i % 26) for i in range(41))
        self.assertEqual(dream._windows(text[:39]), set())
        self.assertEqual(dream._windows(text[:40]), {hash(text[:40])})
        self.assertEqual(dream._windows(text), {hash(text[:40]), hash(text[1:])})

    def test_a_gate_reason_is_read_from_a_json_object_and_falls_back_to_the_raw_text(self):
        self.assertEqual(dream._gate_reason('{"reason": "the gate failed"}'), "the gate failed")
        self.assertEqual(dream._gate_reason('{"reason": null}'), "")
        self.assertEqual(dream._gate_reason("[1]"), "[1]")
        self.assertEqual(dream._gate_reason("not json"), "not json")


class DenylistTests(unittest.TestCase):
    def test_an_unreadable_digest_and_a_raw_gate_record_still_feed_the_denylist(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = make_world(Path(tmp), "20260917-1200-a", "github.com/acme/app", ["build-1", "ship-1"],
                               labelled=False, reason="the gate passed")
            (world / "points" / "build-1" / "digest.json").unlink()
            (world / "points" / "build-1" / "gate.json").write_text("the playwright config pins port 4173 today")
            (world / "points" / "ship-1" / "digest.json").write_text(json.dumps({
                "run": {"branch": "factory/fixture-branch"}, "pr": {"number": 4821}}))
            deny = dream.denylist([world])
        self.assertTrue({"20260917-1200-a", "github.com/acme/app", "fixture", "factory/fixture-branch", "#4821",
                         "pull/4821"} <= deny.literals)
        self.assertTrue(dream._windows("the playwright config pins port 4173 today") <= deny.shingles)

    def test_literals_of_four_characters_or_more_are_kept_from_the_digest_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            world = make_world(Path(tmp), "20260917-1200-a", "github.com/acme/app", ["build-1", "ship-1"],
                               labelled=False)
            (world / "points" / "build-1" / "digest.json").write_text(json.dumps({"run": {"branch": "abc",
                                                                                         "plan": "plan-a"}}))
            (world / "points" / "ship-1" / "digest.json").write_text(json.dumps({"run": {"branch": "wxyz"}}))
            deny = dream.denylist([world])
        self.assertTrue({"plan-a", "wxyz"} <= deny.literals)
        self.assertNotIn("abc", deny.literals)
        self.assertNotIn("", deny.literals)

    def test_the_shingles_are_the_gate_reason_the_agent_message_and_the_event_reason(self):
        gate = "the playwright config pins port 4173 but vite serves on 5173"
        message = '{"reason": "an agent message that happens to be a json object"}'
        with tempfile.TemporaryDirectory() as tmp:
            world = make_world(Path(tmp), "20260917-1200-a", "github.com/acme/app", ["build-1"], labelled=False,
                               reason="the event reason is long enough to leave windows behind")
            point = world / "points" / "build-1"
            (point / "gate.json").write_text(json.dumps({"passed": False, "reason": gate}))
            (point / "last-message.md").write_text(message)
            deny = dream.denylist([world])
        self.assertEqual(deny.shingles, dream._windows(gate) | dream._windows(message)
                         | dream._windows("the event reason is long enough to leave windows behind"))


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
        return call_cli(*argv)

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
            code, _out, err = self.call("dream", "--rounds", "1")
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

    def test_a_dream_session_that_writes_no_skill_is_a_rejected_round(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1"])
        self.dream_step("none")
        code, out, _ = self.call("dream", "--rounds", "1")
        self.assertEqual(code, 0, out)
        self.assertIn("round 1: no candidate (the dream session wrote no SKILL.md)", out)
        self.assertEqual(self.replays(candidate=True), [])
        self.assertEqual(self.decision()["best"], "incumbent")
        self.assertIn("Rejected by `session`: the dream session wrote no SKILL.md", self.report())

    def test_the_call_cap_inside_a_candidate_round_ends_the_dream(self):
        make_world(self.home, "20260917-1200-a", "github.com/acme/app", ["build-1", "ship-1"])
        code, out, _ = self.call("dream", "--rounds", "2", "--max-calls", "3")
        self.assertEqual(code, 0, out)
        decision_ = self.decision()
        self.assertEqual((decision_["partial"], decision_["reason"]), (True, "partial"))
        self.assertEqual(decision_["selection"], {"incumbent": 0.0, "round-1": None})
        self.assertEqual(len(self.replays(candidate=True)), 1)
        self.assertFalse((self.dream_dir() / "rounds" / "2").exists())
        self.assertIn("Selection score - (partial)", self.report())

    def test_rounds_served_counts_earlier_dreams_on_the_same_confirmation_set(self):
        dreams = self.home / "dreams"
        for name, body in (("current", {"split": {"confirmation_hash": "h"}}), ("a", {"split": {"confirmation_hash": "h"}}),
                           ("b", "{"), ("c", {"split": {"confirmation_hash": "other"}}), ("d", [])):
            (dreams / name).mkdir(parents=True)
            (dreams / name / "decision.json").write_text(body if isinstance(body, str) else json.dumps(body))
        self.assertEqual(dream.rounds_served(dreams, "h", dreams / "current"), 1)

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


class PushCauseTests(unittest.TestCase):
    def test_a_push_failure_is_named_by_its_cause(self):
        self.assertEqual(dream.push_cause("! [rejected] main -> main (fetch first)"), "non-fast-forward")
        self.assertEqual(dream.push_cause("fatal: unable to access 'https://x/': Could not resolve host"), "network")
        self.assertEqual(dream.push_cause("remote: hook declined\nerror: failed to push some refs"),
                         "error: failed to push some refs")
        self.assertEqual(dream.push_cause(""), "git push failed")

    def test_an_unknown_cause_is_its_last_line_cut_at_300_characters(self):
        self.assertEqual(dream.push_cause("remote: hook declined\n" + "x" * 301), "x" * 300)


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
        # The incumbent the dream scored: the source skill and the generated policy as they were at its start.
        base = {"text": self.source.read_text(),
                "policy": dream.provenance.tree_policy(self.repo / "plugins" / "factory")["sha256"]}
        self.candidate = {"text": text, "tree": tree, "name": "round-1", "base": base}

    def deploy(self, *, gain: float = 0.06, worlds: int = 6, points: int = 5,
               repositories: tuple = ("github.com/acme/app", "github.com/acme/web"), partial: bool = False,
               enabled: bool = True, winner: bool = True) -> dict:
        views = confirmation_views(worlds, points, list(repositories)) if worlds else []
        scores = {"incumbent": Score(0.5, "incumbent"), "candidate": Score(0.5 + gain)}
        return dream.deploy(dream_dir=self.dream_dir, candidate=self.candidate if winner else None,
                            confirmation=scores, confirmation_worlds=views, partial=partial, cfg=self.cfg,
                            root=self.repo, enabled=enabled, echo=lambda _: None)

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

    def test_no_candidate_over_the_floor_leaves_the_incumbent(self):
        head = self.head()
        decision_ = self.deploy(winner=False)
        self.assertEqual(decision_["reason"], "incumbent")
        self.assertNotIn("source_hash", decision_)
        self.assert_untouched(head)

    def test_no_deploy_stops_a_winner_before_the_checkout_is_touched(self):
        head = self.head()
        self.assertEqual(self.deploy(enabled=False)["reason"], "no_deploy")
        self.assert_untouched(head)

    def test_a_checkout_off_main_refuses_the_deploy(self):
        git(self.repo, "checkout", "-q", "-b", "side")
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual((decision_["reason"], decision_["cause"]), ("branch", "main is not checked out (side)"))
        self.assert_untouched(head)

    def test_an_unreadable_index_refuses_the_deploy_as_dirty(self):
        head = self.head()
        (self.repo / ".git" / "index").write_bytes(b"not an index")
        decision_ = self.deploy()
        self.assertEqual(decision_["reason"], "dirty")
        self.assertIn("index", decision_["cause"])
        self.assertEqual(self.head(), head)
        self.assertEqual((self.source.read_bytes(), self.generated.read_bytes()), self.originals)

    def test_an_unpushed_deploy_that_now_pushes_adds_nothing_new(self):
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "feat(foreman): dream 20260917-0900-dream raises it")
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual((decision_["deployed"], decision_["reason"], decision_["pending"]), (False, "pushed_pending", [head]))
        self.assertEqual(git(self.root / "workflow-origin.git", "rev-parse", "main"), head)
        self.assert_untouched(head)

    def test_local_commits_ahead_of_origin_refuse_the_deploy_and_are_never_pushed(self):
        origin = self.root / "workflow-origin.git"
        published = git(origin, "rev-parse", "main")
        (self.repo / "NOTES.md").write_text("local work nobody asked to publish\n")
        git(self.repo, "add", "NOTES.md")
        git(self.repo, "commit", "--quiet", "-m", "docs: local notes")
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "unpushed_commits"), decision_)
        self.assertIn(head[:12], decision_["cause"])
        self.assertIn("unpushed_commits", __import__("runner.cli", fromlist=["cli"]).DEPLOY_FAILURES)
        self.assertEqual(git(origin, "rev-parse", "main"), published)
        self.assert_untouched(head)

    def test_an_unpushed_deploy_behind_a_local_commit_is_not_pushed_with_it(self):
        origin = self.root / "workflow-origin.git"
        published = git(origin, "rev-parse", "main")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "docs: local notes")
        git(self.repo, "commit", "--quiet", "--allow-empty", "-m", "feat(foreman): dream 20260917-0900-dream raises it")
        head = self.head()
        decision_ = self.deploy()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "unpushed_commits"), decision_)
        self.assertEqual(decision_["pending"], [head])
        self.assertEqual(git(origin, "rev-parse", "main"), published)
        self.assert_untouched(head)

    def land(self, relative: str, line: str) -> str:
        """Someone else's change to a policy file while the dream ran: committed, rebuilt, and pushed."""
        path = self.repo / relative
        path.write_text(path.read_text() + line)
        subprocess.run([sys.executable, "scripts/build_codex_plugin.py", "--plugin", "factory"], cwd=self.repo,
                       check=True, capture_output=True)
        git(self.repo, "commit", "--quiet", "-am", "docs(foreman): a change made while the dream ran")
        git(self.repo, "push", "--quiet", "origin", "main")
        return self.head()

    def assert_kept(self, head: str, relative: str, line: str) -> None:
        self.assertEqual(self.head(), head)
        self.assertEqual(git(self.root / "workflow-origin.git", "rev-parse", "main"), head)
        self.assertIn(line, (self.repo / relative).read_text())
        self.assertNotIn(CANDIDATE_LINE, self.generated.read_text())
        self.assertEqual(git(self.repo, "status", "--porcelain", "--untracked-files=no"), "")

    def test_a_protocol_change_landed_during_the_dream_refuses_the_deploy(self):
        line = "\nA protocol rule added while the dream ran.\n"
        head = self.land("factory/references/factory-run.md", line)
        decision_ = self.deploy()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "incumbent_changed"), decision_)
        self.assertIn("factory-run.md", decision_["cause"])
        self.assertIn("incumbent_changed", __import__("runner.cli", fromlist=["cli"]).DEPLOY_FAILURES)
        self.assert_kept(head, "plugins/factory/references/factory-run.md", line)

    def test_a_skill_change_landed_during_the_dream_is_never_reverted(self):
        line = "\nA skill rule added while the dream ran.\n"
        head = self.land("factory/skills/foreman/SKILL.md", line)
        decision_ = self.deploy()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "incumbent_changed"), decision_)
        self.assertIn("factory/skills/foreman/SKILL.md", decision_["cause"])
        self.assert_kept(head, "factory/skills/foreman/SKILL.md", line)

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
        git(self.repo, "push", "--quiet", "origin", "main")
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
    def test_an_incumbent_replay_outage_is_never_scored_as_a_wrong_answer(self):
        """A Codex outage during the incumbent's confirmation repeats must not buy the candidate a deploy."""
        repo = make_factory_repo(self.root / "workflow")
        os.environ["FACTORY_REPO_ROOT"] = str(repo)
        self.configure(dream_floor_worlds=2, dream_floor_repos=1)
        for n in range(4):
            make_world(self.home, f"20260917-120{n}-a", "github.com/acme/app", ["build-1", "ship-1"])
        # Split by run: worlds 0 and 2 select, 1 and 3 confirm. The candidate wins selection on one point;
        # on confirmation both policies answer alike, but every incumbent call there fails at the host.
        outage = {"mode": "exit", "exit": 1}
        incumbent = {"*": decision("advance"), "20260917-1200-a/build-1": decision("park"),
                     **{f"20260917-120{n}-a/{p}": outage for n in (1, 3) for p in ("build-1", "ship-1")}}
        self.files["replay"].write_text(json.dumps(incumbent))
        self.files["candidate"].write_text(json.dumps({"*": decision("advance")}))
        head = git(repo, "rev-parse", "HEAD")
        code, out, _ = self.call("dream", "--rounds", "1")
        decision_ = self.decision()
        self.assertEqual((decision_["deployed"], decision_["reason"]), (False, "unreplayed"), decision_)
        self.assertEqual(code, 1, out)
        # Two repeats of four points, each tried twice: eight draws failed, sixteen calls.
        self.assertEqual(decision_["unreplayed"], 8)
        self.assertIsNone(decision_["confirmation"]["incumbent"])
        self.assertEqual(decision_["confirmation"]["candidate"], 1.0)
        self.assertIn("8 replay draw(s) failed", self.report())
        self.assertEqual(git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.root / "workflow-origin.git", "rev-parse", "main"), head)

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
