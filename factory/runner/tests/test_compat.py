"""Runs written by an older runner stay inspectable and never pass as newly verified."""

import contextlib
import io
import json
import shutil

from runner import checkpoints, cli
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, happy_scenario
from runner.worker import Worker

NEW_FIELDS = ("completion", "usage", "cost", "timings", "intent", "operations", "conditions")
NEW_ATTEMPT_FIELDS = ("usage", "cost", "checkpoints", "conditions", "code", "fingerprint", "runtime")


def make_legacy(run: Run) -> dict:
    """Strip what the older runner never recorded, as the retained 2026-09-15 run looks."""
    data = json.loads((run.dir / "run.json").read_text())
    for field in NEW_FIELDS:
        data.pop(field, None)
    for attempt in data["attempts"]:
        for field in NEW_ATTEMPT_FIELDS:
            attempt.pop(field, None)
    (run.dir / "run.json").write_text(json.dumps(data, indent=2))
    for tree in ("intent", "checkpoints"):
        shutil.rmtree(run.dir / tree, ignore_errors=True)
    return data


class CompatibilityTests(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(list(argv))
        return code, stdout.getvalue()

    def test_a_legacy_done_run_is_inspectable_exportable_and_reported_as_historical(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        original = make_legacy(run)
        code, shown = self.call("show", run.id)
        self.assertEqual(code, 0)
        self.assertIn("categories not recorded for this run", shown)
        self.assertEqual(self.call("export", run.id)[0], 0)
        self.assertEqual(self.call("export", str(self.home / "exports" / run.id), "--verify")[0], 0)
        self.assertIn("legacy        1 done run(s) predate completion records", self.call("report")[1])
        self.assertEqual(json.loads((run.dir / "run.json").read_text()), original, "inspection rewrote the run")

    def test_resuming_a_legacy_run_refuses_unverifiable_intent_with_a_repair(self):
        self.scenario(happy_scenario())
        run = self.queued_run()
        make_legacy(run)
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "needs-human")
        self.assertIn("predates snapshots", data["human"]["reason"])
        self.assertIn("--rescope", data["human"]["reason"])
        self.assertEqual(len([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"]), 1,
                         "the refusal is at the gate, and nothing after it runs")

    def test_a_newer_checkpoint_version_is_recomputed_not_trusted(self):
        target = checkpoints.path(self.root, "tests")
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({"schema": "factory.checkpoint/2", "fingerprint": "x", "outputs": {}}))
        self.assertEqual(checkpoints.reusable(self.root, "tests", {}),
                         (None, "computed: checkpoint is unreadable or an unsupported version"))
