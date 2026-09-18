"""History worlds: finished runs turned into replayable decision points, without touching the runs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import history, records
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, call_cli
from runner.worker import Worker


def advance(stage: str) -> dict:
    return {"action": "advance", "stage": stage}


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


class HistoryTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        return call_cli(*argv)

    def finished_run(self, *, foreman: str = "off", decisions: list | None = None):
        self.configure(foreman=foreman)
        if decisions is not None:
            self.foreman(decisions)
        run = self.queued_run()
        self.assertEqual(Worker(self.home, run.id, grace=1).run(), 0)
        return run

    def world(self, run_id: str) -> dict:
        return json.loads((self.home / "history" / run_id / "world.json").read_text())


class BuildFromRunsTests(HistoryTestCase):
    def test_a_codex_run_yields_one_recorded_point_per_decided_headless_attempt(self):
        run = self.finished_run(foreman="codex", decisions=[advance("scope-review"), "invalid", advance("build"),
                                                             advance("ship")])
        code, out, _ = self.call("history", "build", run.id)
        self.assertEqual(code, 0, out)
        world = self.world(run.id)
        records.validate_world(world)
        self.assertEqual([p["id"] for p in world["points"]], ["scope-review-1", "build-1", "ship-1"])
        self.assertEqual([p["snapshot"] for p in world["points"]], ["recorded"] * 3)
        self.assertEqual([p["turn"] for p in world["points"]], [1, 3, 4])
        self.assertEqual([p["origin"] for p in world["points"]], ["cold", "warm", "warm"])
        self.assertEqual({p["recorded_decision"]["action"] for p in world["points"]}, {"advance"})
        points = self.home / "history" / run.id / "points"
        self.assertEqual(sorted(p.name for p in points.iterdir()), ["build-1", "scope-review-1", "ship-1"])
        self.assertEqual((points / "build-1" / "digest.json").read_bytes().count(b"factory.digest/1"), 1)
        self.assertTrue((points / "build-1" / "gate.json").is_file())
        self.assertTrue((points / "build-1" / "plan" / "spec.md").is_file())
        self.assertTrue((points / "build-1" / "event.md").read_text().startswith(f"Factory run {run.id}: event"))

    def test_the_world_records_the_skill_hash_its_last_foreman_turn_ran_under(self):
        run = self.finished_run(foreman="codex", decisions=[advance("scope-review"), advance("build"),
                                                             advance("ship")])
        turns = run.dir / "foreman" / "turns"
        last = max((p for p in turns.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
        sha = json.loads((last / "decision.json").read_text())["policy"]["sha256"]
        self.assertTrue(sha)
        self.assertEqual(self.call("history", "build", run.id)[0], 0)
        self.assertEqual(self.world(run.id)["skill_hash"], sha)

    def test_a_run_without_the_foreman_is_rebuilt_with_its_auto_transitions(self):
        run = self.finished_run(foreman="off")
        self.call("history", "build", run.id)
        world = self.world(run.id)
        self.assertEqual([p["snapshot"] for p in world["points"]], ["reconstructed"] * 3)
        for point in world["points"]:
            self.assertEqual(point["recorded_decision"], {"action": "stage_passed", "vocabulary": "transition"})
            self.assertEqual(point["decision_source"], "auto")
            self.assertNotIn("origin", point)
            self.assertTrue(point["scorable"])
        digest = json.loads((self.home / "history" / run.id / "points" / "ship-1" / "digest.json").read_text())
        self.assertEqual([a["stage"] for a in digest["attempts"]], ["scope-review", "build", "ship"])

    def test_the_world_carries_the_outcome_log_the_note_and_every_attempt(self):
        run = self.finished_run()
        (run.dir / "outcomes.jsonl").write_text(json.dumps({"kind": "annotation", "note": f"see {run.worktree}/x"})
                                                + "\n")
        (run.dir / "note").write_text(f"check {run.dir}/attempts/build-1/gate.json\n")
        self.call("history", "build", run.id)
        world_dir = self.home / "history" / run.id
        self.assertIn("see <worktree>/x", (world_dir / "outcomes.jsonl").read_text())
        self.assertEqual((world_dir / "note").read_text(), "check <run_dir>/attempts/build-1/gate.json\n")
        attempts = self.world(run.id)["run"]["attempts"]
        self.assertEqual([(a["stage"], a["n"], a["outcome"]) for a in attempts],
                         [("scope-review", 1, "done"), ("build", 1, "done"), ("ship", 1, "done")])
        self.assertTrue(all(isinstance(a["tokens"], int) for a in attempts))

    def test_all_builds_worlds_from_runs_and_the_archive(self):
        first = self.finished_run()
        second = self.finished_run()
        (self.home / "archive").mkdir(exist_ok=True)
        shutil.move(str(second.dir), str(self.home / "archive" / second.id))
        code, out, _ = self.call("history", "build", "--all")
        self.assertEqual(code, 0, out)
        self.assertIn("History: 2 built, 0 unchanged, 0 skipped.", out)
        self.assertEqual(sorted(p.name for p in history.worlds(self.home)), sorted([first.id, second.id]))
        code, out, _ = self.call("history", "build", "--all")
        self.assertIn("History: 0 built, 2 unchanged, 0 skipped.", out)
        code, listed, _ = self.call("history", "ls")
        self.assertIn(f"{first.id}  3 point(s), 3 scorable, 0 labelled, done, path:", listed)

    def test_a_live_run_is_skipped_with_its_reason(self):
        run = self.queued_run()
        run.data["status"] = "running"
        run.save()
        code, out, _ = self.call("history", "build", run.id)
        self.assertEqual(code, 0)
        self.assertIn(f"{run.id}: skipped, status running is not terminal", out)
        self.assertFalse((self.home / "history" / run.id).exists())

    def test_building_never_writes_inside_the_run_directory(self):
        run = self.finished_run(foreman="codex", decisions=[advance("scope-review"), advance("build"),
                                                             advance("ship")])
        before = tree_hashes(run.dir)
        self.assertEqual(self.call("history", "build", "--all")[0], 0)
        self.assertEqual(tree_hashes(run.dir), before)

    def test_a_second_history_command_waits_for_nobody(self):
        with history.HistoryLock(self.home):
            code, out, err = self.call("history", "build", "--all")
        self.assertEqual(code, 3)
        self.assertIn("history.lock", err)
        self.assertEqual(out, "")


class LabelTests(HistoryTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.hindsight_path = self.root / "hindsight.json"
        os.environ["FACTORY_STUB_HINDSIGHT"] = str(self.hindsight_path)

    def hindsight(self, answer: object) -> None:
        self.hindsight_path.write_text(json.dumps(answer))

    def labeller_calls(self) -> list[dict]:
        return [c for c in self.stub_calls("codex") if c["env"].get("FACTORY_ROLE") == "hindsight"]

    def built_world(self) -> tuple[object, Path]:
        run = self.finished_run(foreman="codex", decisions=[advance("scope-review"), advance("build"),
                                                             advance("ship")])
        self.assertEqual(self.call("history", "build", run.id)[0], 0)
        return run, self.home / "history" / run.id

    def test_one_labeller_session_labels_every_scorable_point(self):
        self.hindsight({"schema": "factory.hindsight/1", "labels": [
            {"point": p, "accept": ["advance"], "reject": ["park"], "allow_override": False,
             "note": "advance: the gate passed"} for p in ("scope-review-1", "build-1", "ship-1")],
            "faults": [{"kind": "harness", "code_family": "record.invalid", "summary": "s", "evidence": [],
                        "attempts": 1, "tokens": None}]})
        run, world_dir = self.built_world()
        code, out, _ = self.call("history", "label", run.id)
        self.assertEqual(code, 0, out)
        labels = records.validate_labels(json.loads((world_dir / "labels.json").read_text()))
        self.assertEqual(sorted(labels["labels"]), ["build-1", "scope-review-1", "ship-1"])
        self.assertEqual({e["label_source"] for e in labels["labels"].values()}, {"model"})
        faults = records.validate_faults(json.loads((world_dir / "faults.json").read_text()))
        self.assertEqual(faults["faults"][0]["kind"], "harness")
        calls = self.labeller_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(Path(calls[0]["cwd"]).resolve(), world_dir.resolve())
        self.assertIn("read-only", calls[0]["argv"])
        self.assertIn("--output-schema", calls[0]["argv"])

    def test_a_human_label_survives_a_relabel_and_model_entries_are_regenerated(self):
        self.hindsight("auto")
        run, world_dir = self.built_world()
        code, out, _ = self.call("history", "label", "--set", run.id, "build-1", "--accept", "regate", "repair",
                                 "--reject", "launch", "--note", "only the result file was wrong")
        self.assertEqual(code, 0, out)
        human = json.loads((world_dir / "labels.json").read_text())["labels"]["build-1"]
        self.assertEqual(human["label_source"], "human")
        self.assertEqual(self.call("history", "label", run.id)[0], 0)
        labels = json.loads((world_dir / "labels.json").read_text())
        stub_value = labels["labels"]["ship-1"]["accept"]
        labels["labels"]["ship-1"]["accept"] = ["cancel"]
        labels["labels"]["ship-1"]["reject"] = []
        (world_dir / "labels.json").write_text(json.dumps(labels))
        before = len(self.labeller_calls())
        code, out, _ = self.call("history", "label", run.id, "--relabel")
        self.assertEqual(code, 0, out)
        self.assertIn("1 human label(s) kept", out)
        self.assertEqual(len(self.labeller_calls()), before + 1)
        after = json.loads((world_dir / "labels.json").read_text())["labels"]
        self.assertEqual(json.dumps(after["build-1"], sort_keys=True), json.dumps(human, sort_keys=True))
        self.assertEqual(after["ship-1"]["accept"], stub_value)
        self.assertEqual(after["ship-1"]["label_source"], "model")

    def test_a_labeller_that_answers_outside_the_schema_twice_leaves_the_world_unlabelled(self):
        self.hindsight("invalid")
        run, world_dir = self.built_world()
        code, out, _ = self.call("history", "label", run.id)
        self.assertEqual(code, 1)
        self.assertIn(f"failed {run.id}: stays unlabelled", out)
        self.assertIn(f"Labelling failed for 1 world(s): {run.id}", out)
        self.assertFalse((world_dir / "labels.json").exists())
        self.assertEqual(len(self.labeller_calls()), 2)

    def test_a_fully_labelled_world_is_unchanged_without_a_labeller_session(self):
        self.hindsight("auto")
        run, _ = self.built_world()
        self.assertEqual(self.call("history", "label", run.id)[0], 0)
        before = len(self.labeller_calls())
        code, out, _ = self.call("history", "label", run.id)
        self.assertEqual(code, 0, out)
        self.assertIn(f"unchanged {run.id}", out)
        self.assertEqual(len(self.labeller_calls()), before)

    def test_label_all_keeps_going_past_a_failed_world(self):
        first, _ = self.built_world()
        second, second_dir = self.built_world()
        self.hindsight({first.id: "invalid", second.id: "auto"})
        code, out, _ = self.call("history", "label", "--all")
        self.assertEqual(code, 1)
        self.assertIn(f"Labelling failed for 1 world(s): {first.id}", out)
        self.assertIn(f"labelled {second.id}", out)
        self.assertTrue((second_dir / "labels.json").is_file())


class HandCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hand-cases-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"
        self.cases = self.root / "cases"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def case(self, name: str, created_at: str, accept: list[str], reject: list[str] | None = None) -> None:
        case = self.cases / name
        case.mkdir(parents=True)
        (case / "digest.json").write_text(json.dumps({"schema": "factory.digest/1",
                                                      "run": {"created_at": created_at}}))
        (case / "expected.json").write_text(json.dumps({"schema": "factory.foreman-eval/1", "stage": "ship",
                                                        "attempt": 4, "accept": accept, "reject": reject or []}))

    def world(self, created_at: str, accept: list[str]) -> None:
        run_dir = fixture_run(self.root, attempts=[attempt("ship", n) for n in (1, 2, 3, 4)])
        data = json.loads((run_dir / "run.json").read_text())
        data["created_at"] = created_at
        (run_dir / "run.json").write_text(json.dumps(data))
        world_dir = history.build(run_dir, self.home).world
        history.set_label(world_dir, "ship-4", accept)

    def test_a_label_wider_than_the_hand_case_is_one_disagreement(self):
        self.world("2026-09-16T16:05:00Z", ["publish", "repair", "regate"])
        self.case("two-ideas-ship-4", "2026-09-16T16:05:00Z", ["publish", "repair"])
        report = history.check_hand_cases(self.home, self.cases)
        self.assertEqual(report["disagreements"], ["two-ideas-ship-4: regate (label accepts, hand does not)"])
        self.assertEqual(report["mean_accept_size"], 3)

    def test_a_case_with_no_world_point_is_reported_unmatched(self):
        self.world("2026-09-16T16:05:00Z", ["publish"])
        self.case("stale-plugin-scope-review-1", "2026-09-16T10:06:00Z", ["repair"])
        report = history.check_hand_cases(self.home, self.cases)
        self.assertEqual(report["unmatched"], ["stale-plugin-scope-review-1"])
        self.assertEqual(report["cases"], 1)

    def test_an_unlabelled_point_is_reported_and_an_unreadable_world_is_passed_over(self):
        run_dir = fixture_run(self.root, attempts=[attempt("ship", n) for n in (1, 2, 3, 4)])
        history.build(run_dir, self.home)
        broken = history.root(self.home) / "20260101-0000-broken"
        broken.mkdir()
        (broken / "world.json").write_text("{not json")
        self.case("fixture-ship-4", "2026-09-17T12:00:00Z", ["publish"])
        report = history.check_hand_cases(self.home, self.cases)
        self.assertEqual(report["matched"], ["fixture-ship-4"])
        self.assertEqual(report["unlabelled"], ["fixture-ship-4"])
        self.assertIsNone(report["mean_accept_size"])


class LabelRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="labels-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.world_dir = history.build(fixture_run(self.root, attempts=[attempt("build", 1)]), self.root / "home").world

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_fault_kind_outside_the_known_kinds_is_rejected(self):
        with self.assertRaises(records.RecordError) as raised:
            records.validate_hindsight({"schema": "factory.hindsight/1", "labels": [], "faults": [
                {"kind": "cosmic-ray", "code_family": "x.y", "summary": "s", "evidence": [], "attempts": 1}]})
        self.assertIn("kind", str(raised.exception))

    def test_a_hand_label_naming_an_unknown_action_is_refused_without_a_write(self):
        history.set_label(self.world_dir, "build-1", ["regate"])
        before = (self.world_dir / "labels.json").read_bytes()
        with self.assertRaises(history.LabelError) as raised:
            history.set_label(self.world_dir, "build-1", ["teleport"])
        self.assertIn("teleport", str(raised.exception))
        self.assertEqual((self.world_dir / "labels.json").read_bytes(), before)

    def test_a_hand_label_with_nothing_accepted_is_refused_without_a_write(self):
        with self.assertRaises(history.LabelError):
            history.set_label(self.world_dir, "build-1", [])
        self.assertFalse((self.world_dir / "labels.json").exists())

    def test_a_hand_label_for_a_point_the_world_lacks_names_the_points_there_are(self):
        with self.assertRaises(history.LabelError) as raised:
            history.set_label(self.world_dir, "ship-9", ["publish"])
        self.assertIn("has no point 'ship-9'; points: build-1", str(raised.exception))
        self.assertFalse((self.world_dir / "labels.json").exists())

    def test_an_unreadable_labels_file_reads_as_unlabelled(self):
        (self.world_dir / "labels.json").write_text("{not json")
        self.assertIsNone(history.load_labels(self.world_dir))


def fixture_run(root: Path, *, attempts: list[dict], conditions: list | None = None, turns: dict | None = None,
                home: Path | None = None) -> Path:
    """A finished run fabricated on disk: run.json, attempt directories with inputs, and optional turn records."""
    run_id = "20260917-1200-fixture"
    run_dir = root / "runs" / run_id
    repo = root / "Users" / "someone" / "project"
    (run_dir / "worktree" / ".dev" / "fixture").mkdir(parents=True)
    repo.mkdir(parents=True)
    for attempt in attempts:
        if not isinstance(attempt.get("n"), int):
            continue
        adir = run_dir / "attempts" / f"{attempt['stage']}-{attempt['n']}"
        (adir / "inputs").mkdir(parents=True)
        (adir / "gate.json").write_text(json.dumps({"passed": False, "reason": f"see {run_dir}/worktree/app.py",
                                                    "home": f"{home or Path.home()}/.config/x"}))
        (adir / "last-message.md").write_text(f"I edited {repo}/app.py in {run_dir}/worktree\n")
        (adir / "inputs" / "spec.md").write_text("# Spec\n")
        (adir / "inputs" / "factory-run.json").write_text(json.dumps({
            "run_id": run_id, "report_dir": f"{run_dir}/reports", "plan_dir": f"{run_dir}/worktree/.dev/fixture"}))
        (adir / "inputs" / "manifest.json").write_text(json.dumps({"schema": "factory.attempt-inputs/1",
                                                                   "run_id": run_id, "stage": attempt["stage"]}))
    for n, turn in (turns or {}).items():
        tdir = run_dir / "foreman" / "turns" / str(n)
        tdir.mkdir(parents=True)
        (tdir / "decision.json").write_text(json.dumps(turn["record"]))
        (tdir / "digest.json").write_text(json.dumps(turn["digest"]))
        (tdir / "prompt.txt").write_text(turn["prompt"])
    data = {"schema": 1, "id": run_id, "repo": str(repo), "remote": "origin", "plan": "fixture", "status": "done",
            "stage": "ship", "created_at": "2026-09-17T12:00:00Z", "retries": {"used": 0, "budget": 5},
            "human": {"reason": None, "since": None, "note": None}, "attempts": attempts,
            "conditions": conditions or [], "stages": {}, "pr": {}, "branch": "factory/fixture"}
    (run_dir / "run.json").write_text(json.dumps(data))
    return run_dir


def attempt(stage: str, n: int, **extra) -> dict:
    return {"stage": stage, "n": n, "kind": "stage", "outcome": "done", "reason": None, "code": None,
            "retryable": False, "source": "gate", "tokens": 100, "started_at": "2026-09-17T12:00:00Z",
            "ended_at": "2026-09-17T12:10:00Z", "transition": {"action": "stage_passed"}, **extra}


class BuildRulesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="history-")
        self.root = Path(os.path.realpath(self._tmp.name))
        self.home = self.root / "home"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_a_hard_stop_point_stays_in_the_world_unscored(self):
        run_dir = fixture_run(self.root, attempts=[
            attempt("build", 1, outcome="blocked", code="secret.found", reason="a key in the diff",
                    transition={"action": "park"}), attempt("ship", 1)])
        result = history.build(run_dir, self.home)
        self.assertEqual(result.status, "built", result.notes)
        points = {p["id"]: p for p in history.load_world(result.world)["points"]}
        self.assertFalse(points["build-1"]["scorable"])
        self.assertIn("secret.found", points["build-1"]["unscorable_reason"])
        self.assertTrue(points["ship-1"]["scorable"])

    def test_the_ssh_and_https_forms_of_a_remote_are_one_repository(self):
        self.assertEqual(history.repo_key("git@github.com:acme/app.git"), history.repo_key("https://github.com/acme/app"))
        self.assertEqual(history.repo_key("https://github.com/Acme/App.git"), "github.com/acme/app")
        self.assertNotEqual(history.repo_key("git@github.com:acme/app.git"), history.repo_key("git@github.com:acme/web"))
        self.assertNotIn("/", history.repo_key("/Users/someone/project").split(":", 1)[1])

    def test_a_path_key_is_the_directory_name_and_ten_hex_characters_of_the_path_hash(self):
        path = str(self.root / "project.git")
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
        self.assertEqual(history.repo_key(path), f"path:project-{digest[:10]}")

    def test_no_absolute_path_survives_and_the_run_id_stays_only_where_the_format_puts_it(self):
        run_dir = fixture_run(self.root, home=Path.home(), attempts=[
            attempt("scope", 1), attempt("build", 1, outcome="blocked", code="evidence.tests_failed",
                                         reason=f"test failed in {self.root}/runs/20260917-1200-fixture/worktree",
                                         foreman={"turn": 1, "dir": "foreman/turns/1"})],
            turns={1: {"record": {"schema": "factory.foreman-turn/2", "turn": 1, "cold": True, "source": "foreman",
                                  "decision": {"action": "regate", "stage": "build"},
                                  "event": {"kind": "attempt.finished", "outcome": "blocked", "files": {}}},
                       "digest": {"schema": "factory.digest/1", "generated_at": "2026-09-17T12:10:00Z",
                                  "run": {"id": "20260917-1200-fixture", "repo": str(self.root / "Users/someone/project")},
                                  "decisions": [{"summary": "retry 20260917-1200-fixture with guidance"}],
                                  "paths": {"run_dir": str(self.root / "runs" / "20260917-1200-fixture"),
                                            "plan_dir": str(self.root / "runs/20260917-1200-fixture/worktree/.dev/fixture")},
                                  "attempts": [{"gate_file": str(self.root / "runs/20260917-1200-fixture/attempts/build-1/gate.json")}]},
                       "prompt": "Follow the skill.\n\nFactory run 20260917-1200-fixture: event attempt.finished.\n"
                                 f"Files: gate {self.root}/runs/20260917-1200-fixture/attempts/build-1/gate.json\n"}})
        result = history.build(run_dir, self.home)
        self.assertEqual(result.status, "built", result.notes)
        world_dir = result.world
        run_id = "20260917-1200-fixture"
        allowed = {"world.json": "run.id", "points/build-1/digest.json": "run.id",
                   "points/build-1/event.md": "opening line", "points/build-1/plan/factory-run.json": "run_id",
                   "points/build-1/plan/manifest.json": "run_id"}
        for path in sorted(p for p in world_dir.rglob("*") if p.is_file()):
            relative = path.relative_to(world_dir).as_posix()
            text = path.read_text()
            with self.subTest(relative):
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/home/", text)
                self.assertNotIn(str(self.root), text)
                self.assertNotIn(str(Path.home()), text)
                if run_id not in text:
                    continue
                self.assertIn(relative, allowed)
                if relative.endswith(".json"):
                    data = json.loads(text)
                    kept = data["run"]["id"] if allowed[relative] == "run.id" else data["run_id"]
                    self.assertEqual(kept, run_id)
                    self.assertEqual(text.count(run_id), 1)
                else:
                    self.assertEqual([line for line in text.splitlines() if run_id in line],
                                     [f"Factory run {run_id}: event attempt.finished."])
        digest = json.loads((world_dir / "points" / "build-1" / "digest.json").read_text())
        self.assertEqual(digest["attempts"][0]["gate_file"], "gate.json")
        self.assertEqual(digest["paths"]["plan_dir"], "plan")
        self.assertEqual(digest["paths"]["run_dir"], "<run_dir>")
        self.assertIn("retry <run_id> with guidance", digest["decisions"][0]["summary"])
        self.assertIn("Files: gate gate.json", (world_dir / "points" / "build-1" / "event.md").read_text())

    def test_a_directory_without_run_json_is_skipped(self):
        (self.root / "runs" / "empty").mkdir(parents=True)
        result = history.build(self.root / "runs" / "empty", self.home)
        self.assertEqual(result.status, "skipped")
        self.assertIn("run.json unreadable", result.notes[0])
        self.assertFalse(history.root(self.home).exists())

    def test_an_unreadable_attempt_is_skipped_and_the_rest_still_builds(self):
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1), {"stage": "build", "outcome": "done"},
                                                   attempt("ship", 1)])
        result = history.build(run_dir, self.home)
        self.assertEqual(result.status, "built")
        self.assertEqual([p["id"] for p in history.load_world(result.world)["points"]], ["build-1", "ship-1"])
        self.assertTrue(any("build-None: attempt skipped, unreadable" in note for note in result.notes), result.notes)

    def test_an_attempt_without_inputs_is_a_planless_scorable_point(self):
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1)])
        shutil.rmtree(run_dir / "attempts" / "build-1" / "inputs")
        result = history.build(run_dir, self.home)
        point = history.load_world(result.world)["points"][0]
        self.assertFalse(point["plan"])
        self.assertTrue(point["scorable"])
        self.assertFalse((result.world / "points" / "build-1" / "plan").exists())
        self.assertIn("no inputs/ directory", " ".join(result.notes))

    def test_a_fallback_turn_without_its_digest_is_reconstructed_and_names_its_missing_files(self):
        gate = self.root / "runs/20260917-1200-fixture/attempts/build-1/gate.json"
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1, foreman={"turn": 1, "dir": "foreman/turns/1"})],
                              turns={1: {"record": {"schema": "factory.foreman-turn/2", "turn": 1, "cold": False,
                                                    "source": "fallback", "decision": None,
                                                    "event": {"kind": "attempt.finished", "outcome": "done", "files": {
                                                        "gate": str(gate), "result": str(self.root / "gone.json")}}},
                                         "digest": {}, "prompt": "Factory run 20260917-1200-fixture: event x.\n"}})
        (run_dir / "foreman" / "turns" / "1" / "digest.json").unlink()
        result = history.build(run_dir, self.home)
        point = history.load_world(result.world)["points"][0]
        self.assertEqual((point["decision_source"], point["origin"], point["snapshot"]),
                         ("fallback", "warm", "reconstructed"))
        self.assertNotIn("recorded_decision", point)
        self.assertEqual(point["event"]["missing_files"], ["result"])
        self.assertTrue((result.world / "points" / "build-1" / "gate.json").is_file())

    def test_an_attempt_whose_turn_record_is_unreadable_is_skipped_with_its_error(self):
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1, foreman={"turn": 1, "dir": "foreman/turns/1"}),
                                                   attempt("ship", 1)],
                              turns={1: {"record": {"turn": "first", "source": "foreman"}, "digest": {}, "prompt": ""}})
        result = history.build(run_dir, self.home)
        self.assertEqual([p["id"] for p in history.load_world(result.world)["points"]], ["ship-1"])
        self.assertTrue(any("build-1: attempt skipped, unreadable (ValueError" in note for note in result.notes),
                        result.notes)

    def test_a_forced_rebuild_keeps_the_labels(self):
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1)])
        world_dir = history.build(run_dir, self.home).world
        label = history.set_label(world_dir, "build-1", ["regate"])
        self.assertEqual(history.build(run_dir, self.home).status, "unchanged")
        self.assertEqual(history.build(run_dir, self.home, force=True).status, "built")
        self.assertEqual(history.load_labels(world_dir)["labels"]["build-1"], label)

    def test_a_json_file_that_does_not_parse_is_copied_as_rewritten_text(self):
        run_dir = fixture_run(self.root, attempts=[attempt("build", 1)])
        (run_dir / "attempts" / "build-1" / "gate.json").write_text(f"truncated {{ {run_dir}/worktree/app.py")
        result = history.build(run_dir, self.home)
        self.assertEqual((result.world / "points" / "build-1" / "gate.json").read_text(),
                         "truncated { <worktree>/app.py")

    def test_without_git_the_repository_is_keyed_by_its_path(self):
        run = Run.load(fixture_run(self.root, attempts=[attempt("build", 1)]))
        with mock.patch.dict(os.environ, {"PATH": ""}):
            key = history.repository(run)
        self.assertEqual(key, history.repo_key(run.data["repo"]))
        self.assertTrue(key.startswith("path:project-"), key)

    def test_a_home_without_history_has_no_worlds(self):
        self.assertEqual(history.worlds(self.home), [])

    def test_redact_takes_paths_the_run_id_and_remote_urls_out_of_a_case(self):
        run = Run.load(fixture_run(self.root, attempts=[attempt("build", 1)]))
        text = (f"{run.worktree}/app.py {run.dir}/note {run.data['repo']} {run.id} {Path.home()}/.config "
                "https://github.com/acme/app/pull/3 https://acme.atlassian.net/browse/APP-12")
        self.assertEqual(history.redact(text, run),
                         "<worktree>/app.py <run_dir>/note <repo> <run_id> <home>/.config "
                         "https://github.com/example/project/pull/3 https://example.atlassian.net/browse/KEY-1")


if __name__ == "__main__":
    unittest.main()
