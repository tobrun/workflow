"""Data-only record parsing: the shared validators, the e2e sidecar, and its consumers."""

import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import FACTORY_ROOT, gates, records

REPO = FACTORY_ROOT.parent
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def sidecar(directory: Path, **overrides) -> Path:
    (directory / "shots").mkdir(parents=True, exist_ok=True)
    (directory / "shots" / "cart.png").write_bytes(PNG)
    record = {
        "schema": "factory.e2e/1", "plan": "checkout", "title": "Checkout", "kind": "frontend",
        "generatedAt": "2026-09-16T10:00:00Z",
        "scenarios": [{
            "id": "S1", "title": "guest checks out", "given": "a cart", "when": "paying", "then": "a receipt",
            "status": "pass",
            "screenshots": [{"step": "cart", "caption": "two books", "file": "shots/cart.png",
                             "sha256": hashlib.sha256(PNG).hexdigest()}],
            "states": [{"step": "pay", "entity": "orders", "before": {"count": 0}, "after": {"count": 1}}],
            "assertions": [{"name": "receipt total is 24.00", "passed": True}],
            "output": "HTTP 201 </script><b>",
        }],
    }
    record.update(overrides)
    path = directory / "checkout-e2e.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def load_script(path: Path):
    spec = importlib.util.spec_from_file_location(f"script_{path.parent.parent.name}_{path.stem}".replace("-", "_"),
                                                  path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class StrictJsonTests(unittest.TestCase):
    def assertCode(self, raw: bytes, code: str, **kwargs) -> records.RecordError:
        with self.assertRaises(records.RecordError) as caught:
            records.load_json_bytes(raw, **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_rejects_non_data(self):
        self.assertCode(b"{kind: 'frontend'}", "record.invalid_json")
        self.assertCode(b'{"a": 1, "a": 2}', "record.duplicate_key")
        self.assertCode(b'{"a": NaN}', "record.non_finite")
        self.assertCode(b'{"a": Infinity}', "record.non_finite")
        self.assertCode(b"[1, 2]", "record.not_object")
        self.assertCode(b'"text"', "record.not_object")
        self.assertCode(b"\xff\xfe", "record.encoding")

    def test_schema_discriminator(self):
        self.assertCode(b'{"schema": "factory.e2e/2"}', "record.unsupported_version", schema="factory.e2e/1")
        self.assertCode(b'{"schema": 1}', "record.schema", schema="factory.e2e/1")
        self.assertEqual(records.load_json_bytes(b'{"schema": "factory.e2e/1"}', schema="factory.e2e/1"),
                         {"schema": "factory.e2e/1"})

    def test_size_is_checked_before_reading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "big.json"
            with path.open("wb") as handle:
                handle.truncate(records.E2E_MAX_BYTES + 1)
            with mock.patch.object(Path, "open", side_effect=AssertionError("read an oversized file")):
                with self.assertRaises(records.RecordError) as caught:
                    records.load_json_file(path, max_bytes=records.E2E_MAX_BYTES)
            self.assertEqual(caught.exception.code, "record.too_large")

    def test_messages_are_bounded(self):
        error = records.RecordError("record.invalid", "x" * 10000)
        self.assertLessEqual(len(str(error)), 400)


class E2eRecordTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_valid_sidecar_and_derived_counts(self):
        record = records.load_e2e(sidecar(self.dir))
        self.assertEqual(records.e2e_counts(record), {"total": 1, "passed": 1, "failed": 0})
        self.assertEqual(records.verify_e2e_evidence(record, self.dir), [])
        record["scenarios"][0]["assertions"][0]["passed"] = False
        self.assertEqual(records.e2e_counts(record), {"total": 1, "passed": 0, "failed": 1})

    def test_invalid_types_duplicates_and_unknown_keys(self):
        cases = [
            ({"kind": "mobile"}, "record.kind"),
            ({"summary": {"total": 9}}, "unknown key(s) 'summary'"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "ok"}]}, "scenarios[0].status"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "pass"}, {"id": "S1", "title": "b", "status": "pass"}]},
             "duplicate scenario id"),
            ({"scenarios": [{"id": "S1", "title": "", "status": "pass"}]}, "scenarios[0].title"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "pass", "durationMs": -1}]}, "durationMs"),
            ({"scenarios": [{"id": "../x", "title": "a", "status": "pass"}]}, "scenarios[0].id"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "pass",
                             "screenshots": [{"step": "s", "file": "/etc/passwd", "sha256": "0" * 64}]}]}, "stay inside"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "pass",
                             "screenshots": [{"step": "s", "file": "a.png", "sha256": "no"}]}]}, "lowercase hex SHA-256"),
            ({"scenarios": [{"id": "S1", "title": "a", "status": "pass",
                             "assertions": [{"name": "n", "passed": "yes"}]}]}, "assertions[0].passed"),
        ]
        for overrides, fragment in cases:
            with self.subTest(fragment):
                with self.assertRaises(records.RecordError) as caught:
                    records.load_e2e(sidecar(self.dir, **overrides))
                self.assertEqual(caught.exception.code, "record.invalid")
                self.assertIn(fragment, str(caught.exception))

    def test_evidence_files_must_exist_and_match(self):
        path = sidecar(self.dir)
        record = records.load_e2e(path)
        (self.dir / "shots" / "cart.png").write_bytes(PNG + b"tampered")
        self.assertIn("does not match its sha256", records.verify_e2e_evidence(record, self.dir)[0])
        (self.dir / "shots" / "cart.png").unlink()
        self.assertIn("does not exist", records.verify_e2e_evidence(record, self.dir)[0])

    def test_r4_expression_in_legacy_report_is_rejected_without_side_effects(self):
        marker = self.dir / "marker"
        report = self.dir / "report.html"
        literal = "{kind: (require('fs').writeFileSync(%r, 'x'), 'non-frontend'), scenarios: []}" % str(marker)
        report.write_text(f"<script>/* E2E_DATA_START */ const E2E_DATA = {literal}; /* E2E_DATA_END */</script>")
        with mock.patch("subprocess.Popen", side_effect=AssertionError("a process was launched")) as popen:
            with self.assertRaises(records.RecordError) as caught:
                records.legacy_e2e_from_html(report)
        self.assertEqual(caught.exception.code, "record.legacy_literal")
        self.assertIn("regenerate the e2e evidence", str(caught.exception))
        self.assertFalse(marker.exists())
        popen.assert_not_called()

    def test_gate_loader_never_runs_a_javascript_runtime(self):
        path = self.dir / "webhook-e2e.json"
        path.write_text("{kind: (require('fs').writeFileSync('m', 'x'), 'frontend')}")
        with mock.patch("subprocess.run", side_effect=AssertionError("run")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("popen")):
            record, error = gates.load_e2e_data(path)
        self.assertIsNone(record)
        self.assertIn("[record.invalid_json]", error)

    def test_gate_failures_are_bounded_and_actionable(self):
        cases = {
            "missing": (None, "record.missing"),
            "malformed": (b'{"schema": "factory.e2e/1", "plan": "p", "kind": "frontend", "scenarios": 3}',
                          "record.invalid"),
            "unsupported": (b'{"schema": "factory.e2e/9"}', "record.unsupported_version"),
            "oversized": (b" " * (records.E2E_MAX_BYTES + 1), "record.too_large"),
        }
        for name, (content, code) in cases.items():
            with self.subTest(name):
                path = self.dir / f"{name}-e2e.json"
                if content is not None:
                    path.write_bytes(content)
                record, error = gates.load_e2e_data(path)
                self.assertIsNone(record)
                self.assertIn(f"[{code}]", error)
                self.assertLess(len(error), 500)

    def test_legacy_strict_json_report_stays_inspectable(self):
        uri = "data:image/png;base64," + base64.b64encode(PNG).decode()
        old = {"title": "Old", "planName": "old", "kind": "frontend", "generatedAt": "2026-09-01",
               "scenarios": [{"id": "a", "title": "t", "status": "pass",
                              "screenshots": [{"step": "s", "caption": "c", "dataUri": uri}],
                              "logsOrOutput": "ok"}],
               "summary": {"total": 1, "passed": 1, "failed": 0}}
        report = self.dir / "old-e2e-report.html"
        report.write_text(f"<script>/* E2E_DATA_START */ const E2E_DATA = {json.dumps(old)}; /* E2E_DATA_END */")
        record = records.legacy_e2e_from_html(report)
        self.assertEqual(record["plan"], "old")
        self.assertEqual(records.e2e_counts(record)["passed"], 1)
        self.assertEqual(record["scenarios"][0]["output"], "ok")


class LayoutTests(unittest.TestCase):
    """Skill scripts load the shared validator from the source tree and from a generated plugin."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def generated_root(self) -> Path:
        builder = load_script(REPO / "scripts" / "build_codex_plugin.py")
        destination = self.dir / "generated" / "factory"
        builder.build(builder.PLUGINS["factory"], destination)
        return destination

    def exercise(self, root: Path) -> None:
        work = self.dir / root.parent.name
        work.mkdir(parents=True, exist_ok=True)
        path = sidecar(work)
        html = work / "report" / "checkout-e2e-report.html"
        render = subprocess.run([sys.executable, str(root / "skills/build/scripts/render-e2e.py"), str(path),
                                 "--out", str(html)], capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(render.stdout)["passed"], 1)
        page = html.read_text()
        self.assertIn('"src": "../shots/cart.png"', page)
        self.assertIn("HTTP 201 \\u003c/script>\\u003cb>", page)
        self.assertNotIn("</script><b>", page)
        self.assertIn("render-e2e.py replaces this block", (root / "skills/build/templates/e2e-report.html").read_text())
        out = work / "evidence"
        extract = subprocess.run([sys.executable, str(root / "skills/ship/scripts/pr-evidence.py"), "extract", str(path),
                                  "--out", str(out), "--url-template", "https://img.example/{path}"],
                                 capture_output=True, text=True)
        self.assertEqual(extract.returncode, 0, extract.stderr)
        evidence = (out / "evidence.md").read_text()
        self.assertIn("1/1 scenarios passed", evidence)
        self.assertIn("![two books](https://img.example/checkout/s1/01-cart.png)", evidence)
        self.assertIn("**Before**", evidence)
        self.assertEqual((out / "checkout" / "s1" / "01-cart.png").read_bytes(), PNG)

    def test_source_layout(self):
        self.exercise(FACTORY_ROOT)

    def test_generated_layout(self):
        self.exercise(self.generated_root())

    def test_extract_rejects_tampered_evidence(self):
        path = sidecar(self.dir)
        (self.dir / "shots" / "cart.png").write_bytes(b"not the captured image")
        result = subprocess.run([sys.executable, str(FACTORY_ROOT / "skills/ship/scripts/pr-evidence.py"), "extract",
                                 str(path), "--out", str(self.dir / "out"), "--url-template", "https://x/{path}"],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn("does not match its sha256 [record.evidence]", result.stderr)


def world(**overrides) -> dict:
    record = {"schema": "factory.world/1", "run": {"id": "r1", "attempts": [
                  {"stage": "build", "n": 1, "outcome": "done", "tokens": 10}]},
              "repository": "/repo", "terminal_status": "done", "decision_schema": "factory.decision/1",
              "source_sha256": "0" * 64, "builder": "test",
              "points": [{"id": "build-1", "stage": "build", "attempt": 1, "event": {}, "scorable": True,
                          "snapshot": "recorded", "plan": False, "origin": "cold", "decision_source": "foreman",
                          "recorded_decision": {"action": "advance", "vocabulary": "decision"}}]}
    record.update(overrides)
    return record


def label(**overrides) -> dict:
    entry = {"accept": ["advance"], "reject": ["park"], "allow_override": False, "note": "the gate passed"}
    entry.update(overrides)
    return entry


class HistoryRecordTests(unittest.TestCase):
    def assertInvalid(self, validate, data: object, needle: str, **kwargs) -> None:
        with self.assertRaises(records.RecordError) as caught:
            validate(data, **kwargs)
        self.assertEqual(caught.exception.code, "record.invalid")
        self.assertIn(needle, str(caught.exception))

    def test_world_accepts_a_replayable_run(self):
        self.assertEqual(records.validate_world(world())["run"]["id"], "r1")

    def test_world_rejections(self):
        point = world()["points"][0]
        cases = [
            ({"run": None}, "world.run: must be an object with an id"),
            ({"run": {"id": "r1", "attempts": ["x"]}}, "world.run.attempts[0]: must be an object"),
            ({"run": {"id": "r1", "attempts": [{"stage": "build"}]}}, "world.run.attempts[0]: missing 'n'"),
            ({"repository": " "}, "world.repository: must be a non-empty string"),
            ({"points": {}}, "world.points: must be a list"),
            ({"points": [1]}, "world.points[0]: must be an object"),
            ({"points": [{**point, "id": "design-1"}]}, "world.points[0].id: must be {stage}-{attempt}"),
            ({"points": [point, point]}, "world.points[1].id: appears twice"),
            ({"points": [{**point, "scorable": "yes"}]}, "world.points[0].scorable: must be true or false"),
            ({"points": [{**point, "snapshot": "guessed"}]}, "world.points[0].snapshot: must be one of"),
            ({"points": [{**point, "origin": "hot"}]}, "world.points[0].origin: must be one of cold, warm"),
            ({"points": [{**point, "decision_source": "human"}]}, "world.points[0].decision_source: must be one of"),
            ({"points": [{**point, "recorded_decision": "advance"}]}, "world.points[0].recorded_decision: must name"),
            ({"points": [{**point, "recorded_decision": {"action": "advance", "vocabulary": "x"}}]},
             "world.points[0].recorded_decision: must name"),
        ]
        for overrides, needle in cases:
            with self.subTest(needle=needle):
                self.assertInvalid(records.validate_world, world(**overrides), needle)
        with self.assertRaises(records.RecordError) as caught:
            records.validate_world({"schema": "factory.world/2"})
        self.assertEqual(caught.exception.code, "record.unsupported_version")

    def test_labels_rejections(self):
        def labels(entries: object) -> dict:
            return {"schema": "factory.labels/1", "run": "r1", "labels": entries}

        ok = label(label_source="model")
        self.assertEqual(records.validate_labels(labels({"build-1": ok}))["run"], "r1")
        cases = [
            (labels([]), "labels.labels: must be an object keyed by point id"),
            (labels({"design-1": ok}), "labels.labels['design-1']: is not a point id"),
            (labels({"build-1": "advance"}), "labels.labels['build-1']: must be an object"),
            (labels({"build-1": label(label_source="oracle")}), "labels.labels['build-1'].label_source: must be one"),
            (labels({"build-1": {**ok, "accept": "advance"}}), ".accept: must be a list of action names"),
            (labels({"build-1": {**ok, "accept": ["fly"]}}), ".accept: unknown action(s) 'fly'; known: launch"),
            (labels({"build-1": {**ok, "accept": []}}), ".accept: must name at least one action"),
            (labels({"build-1": {**ok, "reject": ["park", "park"]}}), ".reject: names an action twice"),
            (labels({"build-1": {**ok, "reject": ["advance"]}}), "['build-1']: accepts and rejects advance"),
            (labels({"build-1": {**ok, "allow_override": "no"}}), ".allow_override: must be true or false"),
            (labels({"build-1": {**ok, "note": ""}}), ".note: must be a non-empty string"),
        ]
        for data, needle in cases:
            with self.subTest(needle=needle):
                self.assertInvalid(records.validate_labels, data, needle)
        with self.assertRaises(records.RecordError) as caught:
            records.validate_labels({"schema": "factory.labels/2"})
        self.assertEqual(caught.exception.code, "record.unsupported_version")

    def test_hindsight_rejections(self):
        def hindsight(labels: object, faults: object = ()) -> dict:
            return {"schema": "factory.hindsight/1", "labels": labels, "faults": list(faults)}

        ok = label(point="build-1")
        fault = {"kind": "stage", "code_family": "gate", "summary": "tests failed", "evidence": [], "attempts": 1}
        self.assertEqual(records.validate_hindsight(hindsight([ok], [fault]), points=["build-1"])["labels"], [ok])
        cases = [
            (hindsight({}), "hindsight.labels: must be a list", None),
            (hindsight([ok, ok]), "hindsight.labels: labels a point twice", None),
            (hindsight(["x"]), "hindsight.labels[0]: must be an object", None),
            (hindsight([]), "hindsight.labels: no label for build-1", ["build-1"]),
            (hindsight([ok, label(point="ship-1")]), "labels unknown or unscorable point(s) ship-1", ["build-1"]),
            ({**hindsight([ok]), "faults": {}}, "hindsight.faults: must be a list", None),
            (hindsight([ok], [{**fault, "kind": "cosmic"}]), "hindsight.faults[0].kind: must be one of", None),
        ]
        for data, needle, points in cases:
            with self.subTest(needle=needle):
                self.assertInvalid(records.validate_hindsight, data, needle, points=points)
        with self.assertRaises(records.RecordError) as caught:
            records.validate_hindsight([])
        self.assertEqual(caught.exception.code, "record.invalid")
        with self.assertRaises(records.RecordError) as caught:
            records.validate_hindsight({"schema": "factory.hindsight/2"})
        self.assertEqual(caught.exception.code, "record.unsupported_version")
        whole = hindsight([ok], [fault])
        self.assertEqual(records.validate_hindsight(whole, points=["build-1"]), whole)

    def test_labels_require_a_note(self):
        note_free = {k: v for k, v in label(label_source="model").items() if k != "note"}
        data = {"schema": "factory.labels/1", "run": "r1", "labels": {"build-1": note_free}}
        self.assertInvalid(records.validate_labels, data, "labels.labels['build-1'].note: required")

    def test_faults_accept_the_bounds(self):
        low = {"kind": "harness", "code_family": "gate", "summary": "tests failed", "evidence": [], "attempts": 0,
               "tokens": 0}
        high = {**low, "evidence": [f"e{n}" for n in range(20)], "attempts": 10000, "tokens": 10 ** 12}
        data = {"schema": "factory.faults/1", "run": "r1", "faults": [low, high]}
        self.assertEqual(records.validate_faults(data), data)

    def test_faults_rejections(self):
        def faults(*entries: object) -> dict:
            return {"schema": "factory.faults/1", "run": "r1", "faults": list(entries)}

        fault = {"kind": "stage", "code_family": "gate", "summary": "tests failed", "evidence": [], "attempts": 1}

        def without(key: str) -> dict:
            return {k: v for k, v in fault.items() if k != key}

        tokens = "must be an integer between 0 and 1000000000000"
        cases = [
            ({**faults(), "faults": {}}, "faults.faults: must be a list"),
            (faults("x"), "faults.faults[0]: must be an object"),
            (faults(without("code_family")), "faults.faults[0].code_family: required"),
            (faults(without("summary")), "faults.faults[0].summary: required"),
            (faults(without("attempts")), "faults.faults[0].attempts: required"),
            (faults({**fault, "evidence": [f"e{n}" for n in range(21)]}), ".evidence: more than 20 entries"),
            (faults({**fault, "attempts": -1}), ".attempts: must be an integer between 0 and 10000"),
            (faults({**fault, "attempts": 10001}), ".attempts: must be an integer between 0 and 10000"),
            (faults({**fault, "tokens": -1}), f".tokens: {tokens}"),
            (faults({**fault, "tokens": 10 ** 12 + 1}), f".tokens: {tokens}"),
        ]
        for data, needle in cases:
            with self.subTest(needle=needle):
                self.assertInvalid(records.validate_faults, data, needle)
        for data in ([], {"schema": "factory.faults/2", "run": "r1", "faults": []}):
            with self.subTest(data=data), self.assertRaises(records.RecordError) as caught:
                records.validate_faults(data)
            self.assertEqual(caught.exception.code, "record.unsupported_version")


if __name__ == "__main__":
    unittest.main()
