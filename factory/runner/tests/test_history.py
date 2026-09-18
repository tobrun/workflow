"""History worlds: finished runs turned into replayable decision points, without touching the runs."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import cli, history, records
from runner.tests.helpers import FactoryTestCase, happy_scenario
from runner.worker import Worker


def advance(stage: str) -> dict:
    return {"action": "advance", "stage": stage}


def tree_hashes(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


class HistoryTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

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


if __name__ == "__main__":
    unittest.main()
