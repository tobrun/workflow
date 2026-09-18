"""Portable exports, post-PR outcomes, and summaries that keep their sample sizes."""

import contextlib
import io
import json
import shutil
import unittest
from pathlib import Path

from runner import cli, outcomes
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, happy_scenario
from runner.worker import Worker


class OutcomeTests(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, stdout.getvalue()

    def done_run(self) -> Run:
        self.scenario(happy_scenario())
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        run = Run.load(run.dir)
        self.assertEqual(run.status, "done", run.data["human"])
        return run

    def set_pr(self, number: int, **fields) -> None:
        state = self.gh()
        next(p for p in state["prs"] if p["number"] == number).update(fields)
        self.gh_state.write_text(json.dumps(state))

    def test_an_export_is_verifiable_elsewhere_and_survives_gc(self):
        run = self.done_run()
        code, shown = self.call("export", run.id, "--comment")
        self.assertEqual(code, 0, shown)
        bundle = self.home / "exports" / run.id
        manifest = json.loads((bundle / "manifest.json").read_text())
        self.assertEqual(manifest["completion"]["revision"], run.data["completion"]["revision"])
        self.assertIn("intent/approved.json", manifest["files"])
        self.assertTrue(any(name.startswith("attempts/ship-1/gate/") for name in manifest["files"]))
        self.assertFalse(any(name.endswith("stdout.jsonl") for name in manifest["files"]))

        moved = self.root / "elsewhere" / "bundle"
        shutil.copytree(bundle, moved)
        self.assertEqual(self.call("export", str(moved), "--verify")[0], 0)
        (moved / "run.json").write_text("{}")
        code, shown = self.call("export", str(moved), "--verify")
        self.assertEqual(code, 1)
        self.assertIn("run.json: checksum mismatch", shown)

        pr = self.gh()["prs"][0]
        self.assertIn(outcomes.digest(bundle), pr["comments"][0])
        self.assertNotIn(".dev/", "".join(self.git_ls_remote_files(run)))

        self.set_pr(pr["number"], state="MERGED", mergedAt="2026-09-16T12:00:00Z")
        shutil.rmtree(bundle)
        self.assertEqual(self.call("gc")[0], 0)
        self.assertFalse(run.dir.exists())
        self.assertEqual(outcomes.verify(self.home / "exports" / run.id), [])

    def git_ls_remote_files(self, run: Run) -> list[str]:
        import subprocess
        return subprocess.run(["git", "-C", str(run.worktree), "ls-tree", "-r", "--name-only", "HEAD"],
                              capture_output=True, text=True, check=True).stdout.splitlines()

    def test_outcomes_append_observations_without_erasing_completion(self):
        run = self.done_run()
        completed = json.loads((run.dir / "run.json").read_text())["completion"]
        code, shown = self.call("outcome", run.id)
        self.assertIn(f"{run.id}: open", shown)
        self.call("outcome", run.id, "--annotate", "rework", "--note", "reviewer asked for idempotency keys",
                  "--eval-case", "evals/bench/tasks/webhook-dedup", "--active-minutes", "25")
        number = self.gh()["prs"][0]["number"]
        self.set_pr(number, state="MERGED", mergedAt="2026-09-16T12:00:00Z", headRefOid_override="f" * 40)
        code, shown = self.call("outcome", run.id)
        self.assertIn(f"{run.id}: merged; the PR head moved after completion", shown)
        self.assertIn("observation open", shown)
        self.assertIn("annotation  rework: reviewer asked for idempotency keys", shown)
        self.assertEqual(json.loads((run.dir / "run.json").read_text())["completion"], completed)
        self.assertEqual([e["kind"] for e in outcomes.read_outcomes(run.dir)], ["observation", "annotation", "observation"])
        self.assertEqual(outcomes.classify({"state": "CLOSED"}), "closed-unmerged")
        self.assertEqual(outcomes.classify(None), "unknown")

        code, shown = self.call("report")
        self.assertIn("success       1/1 (100%) of finished runs", shown)
        self.assertIn("rework        1/1 (100%)", shown)
        self.assertIn("PR outcomes   merged 1", shown)
        self.assertIn("operator time 25 min measured on 1 run(s); unknown for 0", shown)
        summary = json.loads(self.call("report", "--json")[1])
        self.assertEqual(summary["regression"], {"count": 0, "n": 1, "rate": 0.0})


class ForemanExportTests(OutcomeTests):
    def test_an_export_carries_the_foreman_turns_without_raw_streams(self):
        self.configure(foreman="codex")
        self.foreman([{"action": "advance", "stage": stage} for stage in ("scope-review", "build", "ship")])
        run = self.done_run()
        self.assertTrue((run.dir / "foreman" / "turns" / "1" / "stdout.jsonl").is_file())
        (run.dir / "attempts" / "build-1" / "stderr.log").write_text("host noise\n")
        code, shown = self.call("export", run.id)
        self.assertEqual(code, 0, shown)
        files = json.loads((self.home / "exports" / run.id / "manifest.json").read_text())["files"]
        self.assertIn("foreman/turns/1/decision.json", files)
        self.assertIn("foreman/turns/1/digest.json", files)
        self.assertIn("attempts/build-1/stderr.log", files)
        self.assertFalse([name for name in files if name.startswith("foreman/") and name.endswith("stdout.jsonl")])


class ByPolicyTests(unittest.TestCase):
    """`factory report --by-policy`: rates grouped by the policy hash each foreman turn recorded."""

    def make_run(self, root: Path, run_id: str, turns: list[tuple[str | None, str]], *, status: str = "done",
                 overrides: int = 0) -> tuple[Path, Run]:
        run_dir = root / run_id
        decisions = []
        for n, (sha, source) in enumerate(turns, start=1):
            turn_dir = run_dir / "foreman" / "turns" / str(n)
            turn_dir.mkdir(parents=True)
            record = {"schema": "factory.foreman-turn/2" if sha else "factory.foreman-turn/1", "turn": n,
                      "source": source}
            if sha:
                record["policy"] = {"skill": "s", "references": "r", "sha256": sha}
            (turn_dir / "decision.json").write_text(json.dumps(record))
            decisions.append({"turn": n, "source": source, "stage": "build", "attempt": n})
        run_dir.mkdir(parents=True, exist_ok=True)
        data = {"id": run_id, "status": status, "attempts": [], "decisions": decisions,
                "overrides": [{"turn": 1, "stage": "build", "attempt": 1, "gate_code": "a.b",
                               "justification": "j"}] * overrides}
        return run_dir, Run(run_dir, data)

    def test_two_policies_and_an_unhashed_run_make_three_groups(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = [self.make_run(root, "a", [("a" * 64, "foreman"), ("a" * 64, "fallback")], overrides=1),
                    self.make_run(root, "b", [("b" * 64, "foreman")], status="needs-human"),
                    self.make_run(root, "c", [(None, "foreman")])]
            groups = outcomes.by_policy(runs)
        self.assertEqual(sorted(groups), sorted(["a" * 64, "b" * 64, "unknown"]))
        self.assertEqual(groups["a" * 64]["turns"], 2)
        self.assertEqual(groups["a" * 64]["fallback"], {"count": 1, "n": 2, "rate": 0.5})
        self.assertEqual(groups["a" * 64]["override"], {"count": 1, "n": 2, "rate": 0.5})
        self.assertEqual(groups["b" * 64]["intervention"], {"count": 1, "n": 1, "rate": 1.0})
        self.assertEqual(groups["unknown"]["runs"], 1)
