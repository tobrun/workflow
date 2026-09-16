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


if __name__ == "__main__":
    unittest.main()
