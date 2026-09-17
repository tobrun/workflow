"""The foreman in shadow mode: consulted at every attempt, recorded, never applied."""

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from runner import events, hosts, records
from runner.model import Run
from runner.tests.helpers import (GAUNTLET, PR_BODY, SHIP_LENSES, FactoryTestCase, at_head, git, happy_scenario,
                                  review_record, review_step)
from runner.tests.test_recovery import RecoveryTestCase
from runner.worker import Worker


def park() -> dict:
    return {"action": "park", "park": {"reason": "stub parks", "operator_action": "factory retry <id>"}}


def advance(stage: str) -> dict:
    return {"action": "advance", "stage": stage}


def launch(stage: str, guidance: str = "Read the previous gate reason and fix exactly that.", **extra) -> dict:
    return {"action": "launch", "stage": stage, "guidance": guidance, **extra}


HAPPY_DECISIONS = [advance("scope-review"), advance("build"), advance("ship")]


class ForemanTestCase(FactoryTestCase):
    def work(self, run: Run) -> dict:
        self.assertEqual(Worker(self.home, run.id, grace=1).run(), 0)
        return json.loads((run.dir / "run.json").read_text())

    def foreman_calls(self) -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_ROLE") == "foreman"]

    def event_names(self, run: Run) -> list[str]:
        return [e["event"] for e in events.read(run.dir)]


class OffTests(ForemanTestCase):
    def test_off_never_consults_and_records_nothing(self):
        self.configure(foreman="off")
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(self.foreman_calls(), [])
        self.assertNotIn("foreman", data)
        self.assertNotIn("decisions", data)
        self.assertFalse((run.dir / "foreman").exists())


class ShadowTests(ForemanTestCase):
    def test_shadow_consults_one_session_per_run_and_keeps_the_legacy_transition(self):
        self.configure(foreman="shadow")
        self.foreman([park()])
        run = self.queued_run()
        data = self.work(run)
        # The legacy decision still ran the pipeline to the end even though the foreman said park every time.
        self.assertEqual(data["status"], "done")
        self.assertEqual([d["action"] for d in data["decisions"]], ["park", "park", "park"])
        self.assertEqual([d["source"] for d in data["decisions"]], ["foreman"] * 3)
        self.assertTrue(all(d["applied"] is False for d in data["decisions"]))
        self.assertEqual(data["foreman"]["session_id"], "stub-foreman")
        self.assertEqual(data["foreman"]["turns"], 3)
        self.assertEqual(data["foreman"]["usage"]["reported_turns"], 3)
        for attempt in data["attempts"]:
            self.assertEqual(attempt["foreman"]["action"], "park")
            self.assertEqual(attempt["foreman"]["source"], "foreman")
        calls = self.foreman_calls()
        self.assertEqual(len(calls), 3)
        first, second = calls[0]["argv"], calls[1]["argv"]
        self.assertEqual(first[0], "exec")
        self.assertTrue(first[1].startswith("$factory:foreman"))
        self.assertIn("-s", first)
        self.assertEqual(first[first.index("-s") + 1], "read-only")
        self.assertEqual(second[0:2], ["exec", "resume"])
        self.assertEqual(second[2], "stub-foreman")
        self.assertNotIn("-s", second)
        self.assertIn('sandbox_mode="read-only"', second)
        self.assertIn("--output-schema", second)
        self.assertEqual(calls[0]["env"]["FACTORY_TURN"], "1")
        self.assertEqual(calls[2]["env"]["FACTORY_TURN"], "3")
        self.assertEqual(Path(calls[0]["cwd"]).resolve(), run.worktree.resolve())
        # Each turn left its own record; the first one carried the cold-start prompt.
        turn1 = json.loads((run.dir / "foreman" / "turns" / "1" / "decision.json").read_text())
        self.assertTrue(turn1["cold"])
        self.assertEqual(turn1["decision"]["action"], "park")
        prompt1 = (run.dir / "foreman" / "turns" / "1" / "prompt.txt").read_text()
        self.assertIn("digest.json", prompt1)
        self.assertIn("scope-review attempt 1 (stage) ended done", prompt1)
        prompt2 = (run.dir / "foreman" / "turns" / "2" / "prompt.txt").read_text()
        self.assertNotIn("digest.json", prompt2)
        self.assertIn("build attempt 1 (stage) ended done", prompt2)
        names = self.event_names(run)
        self.assertEqual(names.count("foreman.started"), 1)
        self.assertEqual(names.count("foreman.decided"), 3)
        self.assertNotIn("foreman.fallback", names)
        self.assertLess(names.index("stage.finished"), names.index("foreman.decided"))

    def test_the_digest_describes_every_attempt_and_the_caps(self):
        self.configure(foreman="shadow", max_stage_attempts=4)
        self.foreman([park()])
        run = self.queued_run()
        self.work(run)
        digest = json.loads((run.dir / "foreman" / "digest.json").read_text())
        self.assertEqual(digest["schema"], "factory.digest/1")
        self.assertEqual([a["stage"] for a in digest["attempts"]], ["scope-review", "build", "ship"])
        self.assertEqual(digest["attempts"][0]["outcome"], "done")
        self.assertTrue(Path(digest["attempts"][0]["gate_file"]).exists())
        self.assertEqual(digest["caps"]["stages"]["build"]["stage_attempts"], {"used": 1, "max": 4})
        self.assertEqual(digest["caps"]["retries"], {"used": 0, "budget": 5})
        self.assertEqual(digest["run"]["id"], run.id)
        self.assertIn("head", digest["git"])
        self.assertEqual(digest["policy"]["mode"], "shadow")
        self.assertEqual(digest["decisions"][-1]["action"], "park")

    def test_a_failed_attempt_reaches_the_foreman_with_its_reason_and_streak(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, {"result": "omit"}]
        self.scenario(scenario)
        self.configure(foreman="shadow")
        self.foreman([park()])
        run = self.queued_run()
        data = self.work(run)
        # The legacy no-progress rule parks after the second identical failure; the foreman saw both.
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["retries"]["used"], 1)
        prompt2 = (run.dir / "foreman" / "turns" / "2" / "prompt.txt").read_text()
        self.assertIn("scope-review attempt 2 (stage) ended failed [review.missing]", prompt2)
        self.assertIn("Same failure code and unchanged tree 2 attempts in a row", prompt2)
        self.assertIn("Caps: scope-review attempts 2/6", prompt2)
        self.assertEqual(data["decisions"][-1]["applied"], False)


class FallbackTests(ForemanTestCase):
    def assert_fallback(self, run: Run, data: dict, reason_fragment: str) -> None:
        self.assertEqual(data["status"], "done")
        self.assertEqual({d["source"] for d in data["decisions"]}, {"fallback"})
        self.assertIn(reason_fragment, data["attempts"][0]["foreman"]["reason"])
        self.assertIsNone(data["attempts"][0]["foreman"]["action"])
        names = self.event_names(run)
        self.assertEqual(names.count("foreman.fallback"), 3)
        self.assertNotIn("foreman.decided", names)

    def test_an_unknown_action_falls_back_after_a_second_try(self):
        self.configure(foreman="shadow")
        self.foreman(["invalid"])
        run = self.queued_run()
        data = self.work(run)
        self.assert_fallback(run, data, "record.invalid")
        self.assertEqual(len(self.foreman_calls()), 6)
        self.assertEqual(data["foreman"]["turns"], 6)

    def test_a_non_json_answer_falls_back(self):
        self.configure(foreman="shadow", foreman_turns_per_event=1)
        self.foreman(["garbage"])
        run = self.queued_run()
        data = self.work(run)
        self.assert_fallback(run, data, "not JSON")
        self.assertEqual(len(self.foreman_calls()), 3)

    def test_a_crashed_turn_falls_back(self):
        self.configure(foreman="shadow", foreman_turns_per_event=1)
        self.foreman([{"mode": "exit", "exit": 2}])
        run = self.queued_run()
        data = self.work(run)
        self.assert_fallback(run, data, "codex exited 2")

    def test_a_turn_past_its_timeout_falls_back(self):
        self.configure(foreman="shadow", foreman_turns_per_event=1, foreman_turn_timeout_s=0.5)
        self.foreman([{"mode": "timeout", "sleep": 20}])
        run = self.queued_run()
        data = self.work(run)
        self.assert_fallback(run, data, "timeout")

    def test_a_second_try_that_answers_is_a_decision(self):
        self.configure(foreman="shadow")
        self.foreman(["garbage", park(), "garbage", park(), "garbage", park()])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual([d["source"] for d in data["decisions"]], ["foreman"] * 3)
        self.assertEqual([d["turn"] for d in data["decisions"]], [2, 4, 6])


class CodexModeTests(ForemanTestCase):
    """`foreman: codex`: the decision is applied under the runner's caps and hard stops."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def test_advance_may_name_the_finished_stage_or_the_next_one(self):
        self.foreman([advance("build"), advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertNotIn("foreman.rejected", self.event_names(run))

    def test_advance_moves_the_run_through_the_pipeline(self):
        self.foreman(HAPPY_DECISIONS)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual([d["applied"] for d in data["decisions"]], [True, True, True])
        self.assertEqual([a["foreman"]["applied"] for a in data["attempts"]], ["stage_passed"] * 3)
        names = self.event_names(run)
        self.assertEqual(names.count("foreman.decided"), 3)
        self.assertNotIn("foreman.rejected", names)
        self.assertEqual(data["retries"]["used"], 0)

    def test_park_records_the_reason_and_the_operator_action(self):
        self.foreman([park()])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["human"]["reason"], "stub parks")
        self.assertEqual(data["human"]["operator_action"], "factory retry <id>")
        self.assertTrue(data["decisions"][0]["applied"])
        parked = next(e for e in events.read(run.dir) if e["event"] == "run.needs_human")
        self.assertEqual(parked["data"]["operator_action"], "factory retry <id>")
        self.assertEqual(len(data["attempts"]), 1)

    def test_launch_again_carries_guidance_and_effort_into_the_next_attempt(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, scenario["scope-review"][0]]
        self.scenario(scenario)
        self.foreman([launch("scope-review", guidance="Write spec-review_1.md before the result file.", effort="high"),
                      advance("scope-review"), advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["retries"]["used"], 1)
        reviews = [a for a in data["attempts"] if a["stage"] == "scope-review"]
        self.assertEqual([a["outcome"] for a in reviews], ["failed", "done"])
        self.assertEqual(reviews[1]["effort"], "high")
        self.assertEqual(reviews[1]["launch"]["effort"], "high")
        stage_calls = [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_STAGE") == "scope-review"
                       and c["env"].get("FACTORY_ROLE") != "foreman"]
        self.assertIn('model_reasoning_effort="high"', stage_calls[1]["argv"])
        prompt = (run.attempt_dir("scope-review", 2) / "prompt.txt").read_text()
        self.assertIn("The foreman left guidance", prompt)
        context = json.loads((run.attempt_dir("scope-review", 2) / "inputs" / "factory-run.json").read_text())
        self.assertIn("Write spec-review_1.md before the result file.", context["guidance"])
        self.assertIn("## after scope-review attempt 1", context["guidance"])
        self.assertEqual([h["n"] for h in context["history"]], [1])
        self.assertEqual(context["history"][0]["outcome"], "failed")
        build_context = json.loads((run.attempt_dir("build", 1) / "inputs" / "factory-run.json").read_text())
        self.assertIsNone(build_context["guidance"])
        self.assertNotIn("next_launch", data)

    def test_launch_may_go_back_to_an_earlier_stage(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [review_step(index=1), review_step(index=2)]
        self.scenario(scenario)
        self.foreman([advance("scope-review"), launch("scope-review", guidance="Review the spec once more."),
                      advance("scope-review"), advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual([a["stage"] for a in data["attempts"]], ["scope-review", "build", "scope-review", "build", "ship"])
        queued = [e for e in events.read(run.dir) if e["event"] == "run.queued" and e["data"].get("previous") == "build"]
        self.assertEqual(queued[0]["stage"], "scope-review")
        self.assertEqual(data["retries"]["used"], 1)

    def test_advance_on_a_failed_gate_is_refused_and_the_fallback_decides(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, scenario["scope-review"][0]]
        self.scenario(scenario)
        self.foreman([advance("scope-review"), advance("scope-review"), advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        first = data["attempts"][0]
        self.assertIn("advance needs a done outcome", first["foreman"]["rejected"])
        self.assertNotIn("applied", first["foreman"])
        self.assertFalse(data["decisions"][0]["applied"])
        names = self.event_names(run)
        self.assertEqual(names.count("foreman.rejected"), 1)
        self.assertIn("retry.scheduled", names)

    def test_launch_that_skips_a_failed_gate_is_refused(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, scenario["scope-review"][0]]
        self.scenario(scenario)
        self.foreman([launch("build", guidance="skip ahead"), advance("scope-review"), advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertIn("would skip the failed scope-review gate", data["attempts"][0]["foreman"]["rejected"])

    def test_the_stage_attempt_cap_parks_whatever_the_foreman_says(self):
        self.configure(foreman="codex", max_stage_attempts=2)
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}]
        self.scenario(scenario)
        self.foreman([launch("scope-review")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("cap reached: scope-review has used 2 of 2 stage attempts", data["human"]["reason"])
        self.assertEqual(len(data["attempts"]), 2)
        self.assertNotIn("foreman.rejected", self.event_names(run))

    def test_three_identical_failures_park_but_two_do_not(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}]
        self.scenario(scenario)
        self.foreman([launch("scope-review")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(len(data["attempts"]), 3)
        self.assertIn("no progress: scope-review attempts 1, 2 and 3", data["human"]["reason"])
        self.assertEqual(data["retries"]["used"], 2)

    def test_an_exhausted_retry_budget_parks_with_the_reset_command(self):
        self.configure(foreman="codex", max_retries=1)
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}]
        self.scenario(scenario)
        self.foreman([launch("scope-review")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("retry budget exhausted", data["human"]["reason"])
        self.assertEqual(data["human"]["operator_action"], f"factory retry {run.id} --reset-budget")
        self.assertEqual(len(data["attempts"]), 2)
        self.assertNotIn("next_launch", data)

    def test_cancel_and_rescope(self):
        self.foreman([{"action": "cancel", "reason": "the request is obsolete"}])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "cancelled")
        self.assertEqual(data["human"]["reason"], "the request is obsolete")
        self.foreman([{"action": "rescope", "reason": "the premise changed"}])
        run = self.queued_run(plan="second")
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["human"]["reason"], "the premise changed")
        self.assertEqual(data["human"]["operator_action"], f"factory retry {run.id} --rescope")
        parked = next(e for e in events.read(run.dir) if e["event"] == "run.needs_human")
        self.assertTrue(parked["data"]["rescope"])

    def test_a_hard_stop_parks_even_when_the_foreman_advances(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{**scenario["scope-review"][0], "result": {
            "status": "blocked", "reason": "a key is committed",
            "conditions": [{"code": "secret.found", "summary": "an API key in tests", "evidence": ["tests/x.py:3"]}]}}]
        self.scenario(scenario)
        self.foreman([advance("scope-review")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("hard stop secret.found", data["human"]["reason"])
        self.assertEqual(data["attempts"][0]["foreman"]["applied"], "park")

    def test_a_failed_turn_falls_back_to_the_legacy_decision(self):
        self.configure(foreman="codex", foreman_turns_per_event=1)
        self.foreman(["garbage"])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(self.event_names(run).count("foreman.fallback"), 3)
        self.assertFalse(any(d["applied"] for d in data["decisions"]))


def ship_with_a_stale_review() -> dict:
    """The agent reviewed an older revision and pushed nothing: the runner will not publish this as ready."""
    stale = review_record(SHIP_LENSES, revision="0" * 40)
    return {"files": {".dev/{plan}/review_1.md": "# Review 1\n\nVerdict: PASS\n\n## Blockers\n",
                      ".dev/{plan}/pr.md": PR_BODY, ".dev/{plan}/review_1.json": json.dumps(stale)},
            "run": [at_head(".dev/{plan}/gauntlet.json", GAUNTLET)],
            "result": {"status": "blocked", "reason": "push failed", "conditions": []}}


class GateOnlyTests(ForemanTestCase):
    """`regate` and `publish`: the runner judges again without spending an agent attempt."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def work(self, run: Run) -> dict:
        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=lambda seconds: None).run(), 0)
        return json.loads((run.dir / "run.json").read_text())

    def ship_execs(self) -> int:
        return len([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"
                    and c["env"].get("FACTORY_STAGE") == "ship" and c["env"].get("FACTORY_ROLE") != "foreman"])

    def test_regate_after_a_transient_github_failure_finishes_without_another_attempt(self):
        state = self.gh()
        state["fail"] = {"pr view": 2}
        self.gh_state.write_text(json.dumps(state))
        self.foreman([advance("scope-review"), advance("build"), {"action": "regate", "stage": "ship"}, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([(a["kind"], a["outcome"], a["host"]) for a in ships],
                         [("stage", "blocked", "codex"), ("regate", "done", "runner")])
        self.assertEqual(ships[0]["code"], "github.unavailable")
        self.assertEqual(ships[1]["baseline"], ships[0]["baseline"])
        self.assertEqual(self.ship_execs(), 1)
        self.assertEqual(data["retries"]["used"], 0)
        self.assertTrue(any(c["decision"].startswith("reused") for c in ships[1]["checkpoints"]), ships[1]["checkpoints"])
        names = self.event_names(run)
        self.assertIn("regate.scheduled", names)
        self.assertEqual(data["decisions"][2]["action"], "regate")
        self.assertTrue(data["decisions"][2]["applied"])
        self.assertNotIn("next_action", data)

    def test_publish_pushes_the_branch_but_never_opens_an_unreviewed_pull_request(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_with_a_stale_review()]
        self.scenario(scenario)
        self.foreman([advance("scope-review"), advance("build"), {"action": "publish"}, park()])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([(a["kind"], a["outcome"], a["code"]) for a in ships],
                         [("stage", "blocked", "pr.missing"), ("publish", "blocked", "pr.missing")])
        self.assertEqual([(op["kind"], op["result"]) for op in data["operations"]], [("push", "done")])
        self.assertEqual(git(run.worktree, "rev-parse", "origin/factory/webhook"), git(run.worktree, "rev-parse", "HEAD"))
        self.assertEqual(self.gh()["prs"], [])
        self.assertEqual(self.ship_execs(), 1)
        gate = json.loads((run.attempt_dir("ship", 2) / "gate.json").read_text())
        self.assertIn("pushed", gate["gate"]["data"]["publish"])
        self.assertIn("publish.scheduled", self.event_names(run))

    def test_two_gate_only_attempts_in_a_row_are_refused(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_with_a_stale_review()]
        self.scenario(scenario)
        regate = {"action": "regate", "stage": "ship"}
        self.foreman([advance("scope-review"), advance("build"), regate, regate, regate, park()])
        run = self.queued_run()
        data = self.work(run)
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([a["kind"] for a in ships], ["stage", "regate", "regate"])
        self.assertIn("gate-only attempts in a row", ships[2]["foreman"]["rejected"])
        self.assertEqual(self.event_names(run).count("foreman.rejected"), 1)
        # The refused decision fell to the legacy decider, whose own no-progress rule parked the run.
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("no progress", data["human"]["reason"])

    def test_publish_of_another_stage_is_refused(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, scenario["scope-review"][0]]
        self.scenario(scenario)
        self.foreman([{"action": "publish", "stage": "scope-review"}, advance("scope-review"), advance("build"),
                      advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertIn("publish applies to ship only", data["attempts"][0]["foreman"]["rejected"])


class WaitTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")
        state = self.gh()
        state["fail"] = {"pr view": 2}
        self.gh_state.write_text(json.dumps(state))
        os.environ.pop("FACTORY_TEST_FLAG", None)

    def tearDown(self) -> None:
        os.environ.pop("FACTORY_TEST_FLAG", None)
        super().tearDown()

    def test_wait_holds_no_slot_and_regates_once_the_probe_passes(self):
        wait = {"action": "wait", "wait": {"seconds": 600, "probe": {"kind": "env", "value": "FACTORY_TEST_FLAG"},
                                            "then": "regate"}}
        self.foreman([advance("scope-review"), advance("build"), wait, advance("ship")])
        run = self.queued_run()
        naps: list[float] = []

        def sleep(seconds: float) -> None:
            naps.append(seconds)
            os.environ["FACTORY_TEST_FLAG"] = "1"

        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=sleep).run(), 0)
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([a["kind"] for a in ships], ["stage", "regate"])
        self.assertEqual(len(naps), 1)
        self.assertGreaterEqual(data["waits"]["ship"], 0)
        self.assertTrue(data["last_wait"]["passed"])
        self.assertEqual(data["last_wait"]["observed"], "FACTORY_TEST_FLAG is set")
        names = self.event_names(run)
        self.assertLess(names.index("wait.scheduled"), names.index("wait.elapsed"))
        self.assertLess(names.index("wait.elapsed"), names.index("regate.scheduled") if "regate.scheduled" in names
                        else len(names))
        slot_events = [e for e in events.read(run.dir) if e["event"] == "slot.acquired"]
        self.assertEqual(len(slot_events), 4)
        prompt = (run.dir / "foreman" / "turns" / "4" / "prompt.txt").read_text()
        self.assertIn("waited", prompt)
        self.assertIn("probe passed", prompt)
        self.assertNotIn("wait", data)

    def test_the_wait_cap_clamps_and_then_parks(self):
        self.configure(foreman="codex", max_wait_minutes=0.01)
        wait = {"action": "wait", "wait": {"seconds": 600, "probe": {"kind": "none"}, "then": "regate"}}
        self.foreman([advance("scope-review"), advance("build"), wait, wait, advance("ship")])
        run = self.queued_run()
        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=lambda seconds: None).run(), 0)
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("cap reached: ship has waited", data["human"]["reason"])
        scheduled = [e for e in events.read(run.dir) if e["event"] == "wait.scheduled"]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["data"]["seconds"], 1)
        self.assertFalse(data["last_wait"]["passed"])

    def test_a_pause_during_a_wait_takes_effect(self):
        wait = {"action": "wait", "wait": {"seconds": 600, "probe": {"kind": "none"}, "then": "regate"}}
        self.foreman([advance("scope-review"), advance("build"), wait])
        run = self.queued_run()

        def sleep(seconds: float) -> None:
            (run.dir / "pause").write_text("now\n")

        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=sleep).run(), 0)
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "paused")
        self.assertEqual(data["wait"]["stage"], "ship")


class ProbeTests(FactoryTestCase):
    def test_gh_auth_probe_follows_the_host_login(self):
        from runner.worker import probe
        state = self.gh()
        state["auth"] = False
        self.gh_state.write_text(json.dumps(state))
        self.assertFalse(probe({"kind": "gh_auth"})[0])
        state["auth"] = True
        self.gh_state.write_text(json.dumps(state))
        passed, observed = probe({"kind": "gh_auth"})
        self.assertTrue(passed)
        self.assertIn("Logged in", observed)
        self.assertEqual(probe({"kind": "none"}), (False, "no probe; waiting the full time"))
        self.assertFalse(probe({"kind": "url", "value": "http://127.0.0.1:9/"})[0])


def ship_with_a_stale_result() -> dict:
    """The agent did everything and pushed, but wrote a result the runner cannot read."""
    from runner.tests.test_checkpoints import ship_without_publication
    step = ship_without_publication()
    step["result"] = "corrupt"
    return step


class RepairTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def work(self, run: Run) -> dict:
        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=lambda seconds: None).run(), 0)
        return json.loads((run.dir / "run.json").read_text())

    def calls(self, role: str, stage: str = "ship") -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["argv"][0] == "exec"
                and c["env"].get("FACTORY_STAGE") == stage and c["env"].get("FACTORY_ROLE") == role]

    def test_a_repair_rewrites_the_result_and_the_full_gate_judges_it(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_with_a_stale_result()]
        self.scenario(scenario)
        repair = {"action": "repair", "stage": "ship", "guidance": "The evidence is fine; only the result file is wrong.",
                  "repair": {"instruction": "Rewrite .dev/webhook/ship-result.json as a valid schema-2 result.",
                             "checks": ["the result file parses"], "timeout_minutes": 10}}
        self.foreman([advance("scope-review"), advance("build"), repair, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([(a["kind"], a["outcome"], a["code"]) for a in ships],
                         [("stage", "blocked", "result.invalid"), ("repair", "done", None)])
        self.assertEqual(ships[1]["repair"]["timeout_minutes"], 10)
        self.assertLess(ships[1]["deadline_at"] - ships[1]["repair"]["timeout_minutes"] * 60, __import__("time").time() + 3600)
        self.assertEqual(len(self.calls("stage")), 1)
        repairs = self.calls("repair")
        self.assertEqual(len(repairs), 1)
        self.assertEqual(repairs[0]["env"]["FACTORY_REPAIR"], "1")
        self.assertEqual(repairs[0]["env"]["FACTORY_ATTEMPT"], "2")
        prompt = repairs[0]["argv"][1]
        self.assertIn("Rewrite .dev/webhook/ship-result.json", prompt)
        self.assertIn("the result file parses", prompt)
        self.assertNotIn("$factory:", prompt)
        self.assertIn("Do not redo the stage", prompt)
        self.assertEqual(data["retries"]["used"], 0)
        self.assertIn("## after ship attempt 1 (repair)", data["guidance"]["ship"])
        self.assertIn("repair.scheduled", self.event_names(run))
        self.assertEqual(data["pr"]["url"], "https://github.com/stub/repo/pull/1")

    def test_a_scenario_map_repair_is_told_to_keep_contract_compatible_selectors(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_with_a_stale_result()]
        self.scenario(scenario)
        repair = {
            "action": "repair",
            "stage": "ship",
            "repair": {
                "instruction": "Repair .dev/webhook/scenario-map.json and rerun the complete ship gate.",
                "checks": ["all mapped tests are collected"],
            },
        }
        self.foreman([advance("scope-review"), advance("build"), repair, advance("ship")])
        run = self.queued_run()
        self.work(run)
        prompt = self.calls("repair")[0]["argv"][1]
        self.assertIn("verified-scenario-map.json", prompt)
        self.assertIn("accepted by the contract's `tests.run` command", prompt)
        self.assertIn("Vitest test name when the contract invokes pytest", prompt)
        self.assertIn("actual test command", prompt)

    def test_repairs_past_the_cap_are_refused(self):
        self.configure(foreman="codex", max_repairs_per_stage=1)
        scenario = happy_scenario()
        scenario["ship"] = [ship_with_a_stale_review()]
        scenario["ship-repair"] = [{"result": {"status": "blocked", "reason": "still stale", "conditions": []}}]
        self.scenario(scenario)
        repair = {"action": "repair", "stage": "ship", "repair": {"instruction": "re-review HEAD"}}
        self.foreman([advance("scope-review"), advance("build"), repair, repair, park()])
        run = self.queued_run()
        data = self.work(run)
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([a["kind"] for a in ships][:2], ["stage", "repair"])
        self.assertIn("already used 1 of 1 repairs", ships[1]["foreman"]["rejected"])
        self.assertEqual(self.event_names(run).count("foreman.rejected"), 1)


def build_without_a_result() -> dict:
    from runner.tests.helpers import build_step
    return {**build_step(), "result": "omit"}


def ship_blocked_on(condition: dict) -> dict:
    from runner.tests.helpers import ship_step
    step = ship_step()
    step["result"] = {"status": "blocked", "reason": "needs a credential", "conditions": [condition]}
    return step


DONE_SHIP_RESULT = json.dumps({"schema": 2, "stage": "ship", "status": "done", "reason": "credential present now",
                               "conditions": [], "pr_url": "https://github.com/stub/repo/pull/1", "draft": False})


class OverrideTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def work(self, run: Run) -> dict:
        self.assertEqual(Worker(self.home, run.id, grace=1, sleep=lambda seconds: None).run(), 0)
        return json.loads((run.dir / "run.json").read_text())

    def test_an_override_advances_the_stage_and_is_noted_on_the_pull_request(self):
        scenario = happy_scenario()
        scenario["build"] = [build_without_a_result()]
        self.scenario(scenario)
        override = {"action": "advance", "stage": "build", "override": {
            "gate_code": "result.missing", "justification": "the tests and e2e passed at the gate; only the result file is missing",
            "evidence": ["gate.json: passed"]}}
        self.foreman([advance("scope-review"), override, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual([a["outcome"] for a in data["attempts"]], ["done", "blocked", "done"])
        self.assertEqual(data["attempts"][1]["code"], "result.missing")
        self.assertEqual(data["attempts"][1]["foreman"]["applied"], "override")
        self.assertEqual(len(data["overrides"]), 1)
        entry = data["overrides"][0]
        self.assertEqual((entry["stage"], entry["attempt"], entry["gate_code"]), ("build", 1, "result.missing"))
        self.assertEqual(entry["turn"], 2)
        self.assertIn("override.recorded", self.event_names(run))
        self.assertIn("notes 1 override", data["overrides_noted"])
        self.assertIn("## Overrides", (run.plan_dir / "pr.md").read_text())
        body = self.gh()["prs"][0]["body"]
        self.assertIn("## Overrides", body)
        self.assertIn("`result.missing`", body)
        self.assertIn("## Evidence", body)
        self.assertEqual([op["kind"] for op in data["operations"] if op["kind"] == "pr-overrides"], ["pr-overrides"])

    def test_an_override_naming_another_code_is_refused(self):
        scenario = happy_scenario()
        scenario["build"] = [build_without_a_result()]
        self.scenario(scenario)
        override = {"action": "advance", "stage": "build", "override": {"gate_code": "evidence.wrong_layer",
                                                                        "justification": "j"}}
        self.foreman([advance("scope-review"), override])
        run = self.queued_run()
        data = self.work(run)
        builds = [a for a in data["attempts"] if a["stage"] == "build"]
        self.assertIn("override names 'evidence.wrong_layer' but this attempt failed with result.missing",
                      builds[0]["foreman"]["rejected"])
        self.assertNotIn("overrides", data)

    def test_the_override_cap_refuses_a_second_one(self):
        self.configure(foreman="codex", max_overrides_per_run=0)
        scenario = happy_scenario()
        scenario["build"] = [build_without_a_result()]
        self.scenario(scenario)
        override = {"action": "advance", "stage": "build", "override": {"gate_code": "result.missing", "justification": "j"}}
        self.foreman([advance("scope-review"), override])
        run = self.queued_run()
        data = self.work(run)
        builds = [a for a in data["attempts"] if a["stage"] == "build"]
        self.assertIn("override refused: 0 of 0 overrides already used", builds[0]["foreman"]["rejected"])

    def test_the_foreman_resolves_a_condition_it_can_vouch_for_and_fixes_the_result_by_hand(self):
        scenario = happy_scenario()
        scenario["ship"] = [ship_blocked_on({"code": "environment.missing_credentials",
                                             "summary": "STRIPE_KEY is not available", "evidence": ["401"]})]
        self.scenario(scenario)
        regate = {"action": "regate", "stage": "ship",
                  "resolve_conditions": [{"id": "C1", "evidence": ["the operator exported the key; gh works"]}],
                  "files": {".dev/webhook/ship-result.json": DONE_SHIP_RESULT}}
        self.foreman([advance("scope-review"), advance("build"), regate, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual([(a["kind"], a["outcome"], a["code"]) for a in ships],
                         [("stage", "blocked", "environment.missing_credentials"), ("regate", "done", None)])
        condition = data["conditions"][0]
        self.assertEqual((condition["id"], condition["status"], condition["resolution"]["by"]), ("C1", "resolved", "foreman"))
        self.assertEqual(condition["resolution"]["evidence"], ["the operator exported the key; gh works"])
        self.assertEqual(ships[0]["foreman"]["hands"]["touched"], [])
        self.assertEqual(ships[0]["foreman"]["hands"]["committed"], [])

    def test_a_resolution_the_runner_can_disprove_is_refused(self):
        os.environ.pop("FACTORY_TEST_MISSING", None)
        scenario = happy_scenario()
        scenario["ship"] = [ship_blocked_on({"code": "environment.missing_credentials", "summary": "key missing",
                                             "evidence": ["401"], "requires_env": ["FACTORY_TEST_MISSING"]})]
        self.scenario(scenario)
        regate = {"action": "regate", "stage": "ship", "resolve_conditions": [{"id": "C1", "evidence": ["trust me"]}],
                  "files": {".dev/webhook/ship-result.json": DONE_SHIP_RESULT}}
        self.foreman([advance("scope-review"), advance("build"), regate, park()])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertEqual(data["conditions"][0]["status"], "unresolved")
        warnings = [e["data"]["warning"] for e in events.read(run.dir) if e["event"] == "stage.warning"]
        self.assertTrue(any("C1 stays open" in w for w in warnings), warnings)
        ships = [a for a in data["attempts"] if a["stage"] == "ship"]
        self.assertEqual(ships[1]["code"], "environment.missing_credentials")


class HandsTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def test_edits_outside_dev_are_reverted_and_the_decision_refused(self):
        self.configure(foreman="codex", foreman_turns_per_event=1)
        self.foreman([{**advance("scope-review"), "files": {"webhook.py": "print('the foreman was here')\n"}},
                      advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        first = data["attempts"][0]["foreman"]
        self.assertEqual(first["source"], "fallback")
        self.assertIn("changed files outside .dev/ (webhook.py); reverted", first["reason"])
        self.assertEqual(first["hands"]["touched"], ["webhook.py"])
        self.assertNotIn("foreman was here", (run.worktree / "webhook.py").read_text())
        self.assertEqual(git(run.worktree, "status", "--porcelain"), "")
        rejected = [e for e in events.read(run.dir) if e["event"] == "foreman.rejected"]
        self.assertEqual(rejected[0]["data"]["reverted"], ["webhook.py"])
        turn1 = json.loads((run.dir / "foreman" / "turns" / "1" / "decision.json").read_text())
        self.assertEqual(turn1["sandbox"], "workspace-write")

    def test_a_commit_outside_dev_parks_the_run_as_a_boundary_violation(self):
        self.foreman([{**advance("scope-review"), "files": {"webhook.py": "print('x')\n"},
                       "run": [["git", "commit", "-qam", "foreman commit"]]}])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("boundary.violation: turn 1 committed outside .dev/: webhook.py", data["human"]["reason"])
        self.assertIn("inspect the commit", data["human"]["operator_action"])
        self.assertEqual(data["attempts"][0]["foreman"]["hands"]["committed"], ["webhook.py"])

    def test_shadow_turns_have_no_hands(self):
        self.configure(foreman="shadow")
        self.foreman([park()])
        run = self.queued_run()
        self.work(run)
        turn1 = json.loads((run.dir / "foreman" / "turns" / "1" / "decision.json").read_text())
        self.assertEqual(turn1["sandbox"], "read-only")
        self.assertNotIn("hands", turn1["extra"])


class LifecycleTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def is_cold(self, call: dict) -> bool:
        return call["argv"][1] != "resume"

    def test_a_changed_thread_id_is_adopted_and_counted_as_a_restart(self):
        self.foreman([advance("scope-review"), {**advance("build"), "thread_id": "stub-2"}, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["foreman"]["session_id"], "stub-2")
        self.assertEqual(data["foreman"]["restarts"], 1)
        calls = self.foreman_calls()
        self.assertEqual(calls[2]["argv"][1:3], ["resume", "stub-2"])
        restarted = [e for e in events.read(run.dir) if e["event"] == "foreman.restarted"]
        self.assertIn("answered as stub-2", restarted[0]["data"]["reason"])

    def test_context_growth_restarts_the_session_from_the_digest(self):
        self.configure(foreman="codex", foreman_context_tokens=100)
        self.foreman(HAPPY_DECISIONS)
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done")
        calls = self.foreman_calls()
        self.assertEqual([self.is_cold(c) for c in calls], [True, True, True])
        self.assertEqual(data["foreman"]["restarts"], 2)
        self.assertIn("digest.json", calls[1]["argv"][1])
        restarted = [e for e in events.read(run.dir) if e["event"] == "foreman.restarted"]
        self.assertIn("input tokens across its model calls", restarted[0]["data"]["reason"])

    def test_a_failed_resume_restarts_from_the_digest_on_the_second_try(self):
        self.foreman([advance("scope-review"), {"mode": "exit", "exit": 1}, advance("build"), advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        calls = self.foreman_calls()
        self.assertEqual([self.is_cold(c) for c in calls], [True, False, True, False])
        self.assertEqual(data["foreman"]["restarts"], 1)
        self.assertEqual([d["turn"] for d in data["decisions"]], [1, 3, 4])
        self.assertEqual(data["attempts"][1]["foreman"]["restarted"][:9], "resume of")

    def test_two_turns_without_a_decision_restart_the_session_once(self):
        self.configure(foreman="codex", foreman_turns_per_event=1)
        self.foreman(["garbage"])
        run = self.queued_run()
        data = self.work(run)
        calls = self.foreman_calls()
        self.assertEqual([self.is_cold(c) for c in calls], [True, False, True])
        self.assertEqual(data["foreman"]["restarts"], 1)
        self.assertTrue(data["foreman"]["restarted_after_fallbacks"])


class OperatorSurfaceTests(ForemanTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")

    def call(self, *argv: str) -> tuple[int, str]:
        import contextlib
        import io
        from runner import cli
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, stdout.getvalue()

    def test_show_and_foreman_print_the_session_decisions_and_caps(self):
        scenario = happy_scenario()
        scenario["build"] = [build_without_a_result()]
        self.scenario(scenario)
        override = {"action": "advance", "stage": "build", "override": {"gate_code": "result.missing",
                                                                        "justification": "evidence passed at the gate"}}
        self.foreman([advance("scope-review"), override, advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        code, shown = self.call("show", run.id)
        self.assertEqual(code, 0)
        self.assertIn("caps       ship attempts 1/6, repairs 0/3, wait 0.0/120 min", shown)
        self.assertIn("foreman    codex mode, session stub-foreman, 3 turn(s), 0 restart(s)", shown)
        self.assertIn("build-1 turn 2: advance (applied): stub decision", shown)
        self.assertIn("build-1 result.missing (turn 2): evidence passed at the gate", shown)
        self.assertIn("foreman turn 2: advance -> override", shown)
        code, listed = self.call("foreman", run.id)
        self.assertEqual(code, 0)
        self.assertIn("Foreman of", listed)
        self.assertIn("turn   1  scope-review-1  advance (applied)", listed)
        code, turn = self.call("foreman", run.id, "--turn", "2")
        self.assertEqual(code, 0)
        self.assertIn("build attempt 1 (stage) ended blocked [result.missing]", turn)
        self.assertIn('"gate_code": "result.missing"', turn)
        code, as_json = self.call("foreman", run.id, "--json")
        self.assertEqual(json.loads(as_json)["caps"]["overrides"]["used"], 1)

    def test_a_parked_run_shows_the_operator_action_as_the_next_command(self):
        self.foreman([{"action": "park", "park": {"reason": "the sandbox has no network",
                                                   "operator_action": "factory retry <id> --note 'network is back'"}}])
        run = self.queued_run()
        self.work(run)
        code, shown = self.call("show", run.id)
        self.assertIn("action     factory retry <id> --note 'network is back'", shown)
        self.assertIn("> factory retry <id> --note 'network is back'", shown)
        from runner import watch
        entries = events.read(run.dir)
        lines = [watch.event_line(Run.load(run.dir), e) for e in entries]
        self.assertTrue(any("foreman: park after scope-review attempt 1: stub decision" in (l or "") for l in lines), lines)
        self.assertTrue(any("next: factory retry <id>" in (l or "") for l in lines), lines)

    def test_reset_budget_restarts_the_caps(self):
        self.configure(foreman="codex", max_stage_attempts=2)
        scenario = happy_scenario()
        scenario["scope-review"] = [{"result": "omit"}, {"result": "omit"}, review_step()]
        self.scenario(scenario)
        self.foreman([launch("scope-review"), launch("scope-review"), advance("scope-review"), advance("build"),
                      advance("ship")])
        run = self.queued_run()
        data = self.work(run)
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("cap reached", data["human"]["reason"])
        code, out = self.call("retry", run.id, "--reset-budget", "--detach")
        self.assertEqual(code, 0, out)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(data["caps_base"]["scope-review"], 2)
        self.assertEqual(len([a for a in data["attempts"] if a["stage"] == "scope-review"]), 3)


class EvalCaseTests(unittest.TestCase):
    """Every captured eval case is well formed and names only actions the decision schema knows."""

    CASES = Path(__file__).resolve().parents[2] / "evals" / "foreman" / "cases"

    def test_cases_are_well_formed_and_redacted(self):
        cases = sorted(p for p in self.CASES.iterdir() if p.is_dir())
        self.assertGreaterEqual(len(cases), 5)
        for case in cases:
            with self.subTest(case.name):
                digest = json.loads((case / "digest.json").read_text(encoding="utf-8"))
                self.assertEqual(digest["schema"], "factory.digest/1")
                self.assertTrue(digest["attempts"])
                event = (case / "event.md").read_text(encoding="utf-8")
                self.assertIn("event attempt.finished", event)
                expected = json.loads((case / "expected.json").read_text(encoding="utf-8"))
                self.assertEqual(expected["schema"], "factory.foreman-eval/1")
                self.assertTrue(set(expected["accept"]) <= set(records.DECISION_ACTIONS), expected["accept"])
                self.assertTrue(set(expected.get("reject", [])) <= set(records.DECISION_ACTIONS))
                self.assertFalse(set(expected["accept"]) & set(expected.get("reject", [])))
                self.assertEqual(digest["attempts"][-1]["stage"], expected["stage"])
                self.assertEqual(digest["attempts"][-1]["n"], expected["attempt"])
                for text in (json.dumps(digest), event):
                    self.assertNotIn("/Users/", text)
                    self.assertNotIn("/home/", text)


class UndecidedAttemptTests(FactoryTestCase):
    def running_after_handoff(self) -> Run:
        """The window right after the handoff: running at scope-review, no attempt begun, scope closed without a transition."""
        run = self.queued_run()
        run.data["attempts"].append({"stage": "scope", "n": 1, "host": "claude", "model": "fable", "effort": "medium",
                                     "started_at": run.data["created_at"], "ended_at": run.data["created_at"],
                                     "outcome": "done", "reason": None, "retryable": True, "source": "gate",
                                     "tokens": 0, "seconds": 1})
        run.transition("slot_acquired")
        run.save()
        return run

    def test_the_scope_attempt_is_never_taken_for_an_undecided_headless_attempt(self):
        run = self.running_after_handoff()
        self.assertIsNone(run.undecided_attempt())
        from runner.worker import reconcile_orphan
        from runner import config
        self.assertEqual(reconcile_orphan(run, config.load(self.home)), "requeued")
        self.assertEqual(run.status, "queued")

    def test_the_watch_view_renders_that_window(self):
        from runner import watch
        run = self.running_after_handoff()
        rows = watch.stage_rows(run, watch.Style(False), watch.StreamTail(), 0.0)
        self.assertTrue(any("scope-review" in row and "running" in row for row in rows), rows)


class DecisionRecoveryTests(RecoveryTestCase):
    """A closed attempt is decided exactly once, whichever side of the decision the worker dies on."""

    def setUp(self) -> None:
        super().setUp()
        self.configure(foreman="codex")
        self.foreman(HAPPY_DECISIONS)

    def foreman_calls(self) -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_ROLE") == "foreman"]

    def test_a_crash_after_closing_the_attempt_decides_it_on_resume_without_another_attempt(self):
        run = self.queued_run()
        self.start(run, fault="worker.after_attempt_close:scope-review:1")
        data = self.wait_dead(run)
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["attempts"][0]["outcome"], "done")
        self.assertNotIn("transition", data["attempts"][0])
        self.assertEqual(self.foreman_calls(), [])
        self.assertIn("(worker down", self.call("ls")[1])
        code, out, _ = self.call("resume", run.id, "--detach")
        self.assertEqual(code, 0, out)
        self.assertIn("its next step was never chosen", out)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(len(self.codex_execs("scope-review")), 1)
        self.assertEqual(len(self.finished_events(run, "scope-review")), 1)
        self.assertEqual([d["turn"] for d in data["decisions"]], [1, 2, 3])

    def test_a_crash_after_the_foreman_turn_reuses_its_decision(self):
        run = self.queued_run()
        self.start(run, fault="worker.after_foreman_turn:scope-review:1")
        data = self.wait_dead(run)
        self.assertEqual(data["status"], "running")
        self.assertEqual(data["attempts"][0]["foreman"]["action"], "advance")
        self.assertEqual(len(self.foreman_calls()), 1)
        self.start(run)
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"), timeout=90)
        self.assertEqual(data["status"], "done", data["human"])
        self.assertEqual(len(self.foreman_calls()), 3)
        self.assertEqual(len(self.codex_execs("scope-review")), 1)
        self.assertEqual([d["turn"] for d in data["decisions"]], [1, 2, 3])
        self.assertEqual(data["foreman"]["session_id"], "stub-foreman")
        names = [e["event"] for e in events.read(run.dir)]
        self.assertEqual(names.count("foreman.started"), 1)
        self.assertEqual(len(self.finished_events(run, "scope-review")), 1)


class DefaultModeTests(unittest.TestCase):
    def test_the_foreman_is_on_by_default(self):
        from runner import config
        self.assertEqual(config.parse({}).foreman, "codex")
        self.assertEqual(config.parse({"foreman": "off"}).foreman, "off")


class DecisionRecordTests(unittest.TestCase):
    def decision(self, **fields) -> dict:
        return {"schema": "factory.decision/1", "summary": "s", "rationale": "r", **fields}

    def test_each_action_accepts_its_own_fields(self):
        cases = [
            self.decision(action="launch", stage="build", guidance="try again", effort="high", model="m", timeout_s=600),
            self.decision(action="repair", stage="ship", guidance="g", effort="high", model="m",
                          repair={"instruction": "fix the result file", "checks": ["schema"], "timeout_minutes": 120}),
            self.decision(action="regate", stage="ship", resolve_conditions=[{"id": "C1", "evidence": ["e"]}]),
            self.decision(action="publish"),
            self.decision(action="wait", wait={"seconds": 60, "probe": {"kind": "gh_auth"}, "then": "regate"}),
            self.decision(action="wait", wait={"seconds": 60, "probe": {"kind": "env", "value": "GH_TOKEN"}, "then": "launch"}),
            self.decision(action="advance", stage="build",
                          override={"gate_code": "evidence.wrong_layer", "justification": "j", "evidence": []}),
            self.decision(action="park", park={"reason": "r", "operator_action": "a"}),
            self.decision(action="rescope", reason="premise changed"),
            self.decision(action="cancel", reason="pointless"),
        ]
        for case in cases:
            with self.subTest(action=case["action"]):
                self.assertEqual(records.validate_decision(case)["action"], case["action"])

    def test_nulls_mean_absent(self):
        data = self.decision(action="publish", stage=None, guidance=None, wait=None, repair=None, override=None,
                             resolve_conditions=None, park=None, reason=None, model=None, effort=None, timeout_s=None)
        self.assertEqual(records.validate_decision(data), self.decision(action="publish"))

    def test_rejections(self):
        bad = [
            ("unknown action", self.decision(action="fly"), "record.invalid"),
            ("field owned by another action", self.decision(action="park", park={"reason": "r", "operator_action": "a"},
                                                             guidance="g"), "record.invalid"),
            ("missing required", self.decision(action="launch", stage="build"), "record.invalid"),
            ("unknown key", self.decision(action="publish", extra=1), "record.invalid"),
            ("bad stage", self.decision(action="regate", stage="scope"), "record.invalid"),
            ("wait too short", self.decision(action="wait", wait={"seconds": 5, "probe": {"kind": "none"}, "then": "regate"}),
             "record.invalid"),
            ("env probe without a name", self.decision(action="wait", wait={"seconds": 60, "probe": {"kind": "env"},
                                                                             "then": "regate"}), "record.invalid"),
            ("override code shape", self.decision(action="advance", stage="build",
                                                  override={"gate_code": "nope", "justification": "j"}), "record.invalid"),
            ("resolution without evidence", self.decision(action="regate", stage="build",
                                                          resolve_conditions=[{"id": "C1", "evidence": []}]), "record.invalid"),
            ("wrong schema", {**self.decision(action="publish"), "schema": 1}, "record.unsupported_version"),
            ("summary too long", self.decision(action="publish", summary="x" * 121), "record.invalid"),
        ]
        for label, data, code in bad:
            with self.subTest(label):
                with self.assertRaises(records.RecordError) as raised:
                    records.validate_decision(data)
                self.assertEqual(raised.exception.code, code)

    def test_output_schema_is_strict_and_names_every_known_field(self):
        schema = records.decision_json_schema()
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["properties"]), {"schema", "action", "summary", "rationale", *records.DECISION_OWNERS})
        self.assertEqual(schema["required"], list(schema["properties"]))
        self.assertEqual(schema["properties"]["action"]["enum"], list(records.DECISION_ACTIONS))
        for name, sub in schema["properties"].items():
            for nested in sub.get("anyOf", [sub]):
                if nested.get("type") == "object":
                    self.assertEqual(nested["required"], list(nested["properties"]), name)
                    self.assertFalse(nested["additionalProperties"], name)
        # Every field an owner table mentions must be optional in the schema, since another action leaves it null.
        for name in records.DECISION_OWNERS:
            sub = schema["properties"][name]
            nullable = "null" in (sub.get("type") or []) or any(alt.get("type") == "null" for alt in sub.get("anyOf", []))
            self.assertTrue(nullable, name)


class ArgvTests(unittest.TestCase):
    def test_new_session_uses_flags_and_resume_uses_config_overrides(self):
        common = dict(prompt="p", model="m", effort="medium", worktree=Path("/wt"), writable=[Path("/w1")],
                      last_message=Path("/o"), schema=Path("/s"))
        fresh = hosts.codex_foreman_argv(sandbox="workspace-write", **common)
        self.assertEqual(fresh[1:3], ["exec", "p"])
        self.assertIn("--add-dir", fresh)
        self.assertEqual(fresh[fresh.index("-C") + 1], "/wt")
        self.assertEqual(fresh[fresh.index("-s") + 1], "workspace-write")
        resumed = hosts.codex_foreman_argv(sandbox="workspace-write", thread_id="t1", **common)
        self.assertEqual(resumed[1:4], ["exec", "resume", "t1"])
        for flag in ("-s", "-C", "--add-dir"):
            self.assertNotIn(flag, resumed)
        self.assertIn('sandbox_mode="workspace-write"', resumed)
        self.assertIn('sandbox_workspace_write.writable_roots=["/wt", "/w1"]', resumed)
        self.assertEqual(resumed[-4:], ["-o", "/o", "--output-schema", "/s"])
        readonly = hosts.codex_foreman_argv(sandbox="read-only", thread_id="t1", **common)
        self.assertNotIn("sandbox_workspace_write.network_access=true", readonly)
        with self.assertRaises(ValueError):
            hosts.codex_foreman_argv(sandbox="bypass", **common)


if __name__ == "__main__":
    unittest.main()
