"""Run real unittest cases, capture JUnit receipts, and verify scenario evidence."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CHECK = ROOT / 'dev/skills/build/scripts/check-tests.py'
RUNNER = '''import sys, unittest, xml.etree.ElementTree as ET
suite = unittest.defaultTestLoader.loadTestsFromName("tests")
class Result(unittest.TextTestResult):
    def startTest(self, test):
        super().startTest(test)
        module, name = test.id().rsplit(".", 1)
        self.case = ET.SubElement(report, "testcase", classname=module, name=name)
    def addFailure(self, test, err):
        super().addFailure(test, err)
        ET.SubElement(self.case, "failure").text = self._exc_info_to_string(err, test)
    def addError(self, test, err):
        super().addError(test, err)
        ET.SubElement(self.case, "error").text = self._exc_info_to_string(err, test)
    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        ET.SubElement(self.case, "skipped").text = reason
report = ET.Element("testsuite")
result = unittest.TextTestRunner(resultclass=Result).run(suite)
ET.ElementTree(report).write(sys.argv[1])
sys.exit(0 if result.wasSuccessful() else 1)
'''


class ScenarioEvidenceTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.plan = self.root / '.dev/coupon'
        self.plan.mkdir(parents=True)
        (self.root / 'app.py').write_text('def total(price):\n    return price * 2\n')
        (self.root / 'tests.py').write_text('import unittest\nfrom app import total\n'
                                           'class CouponTest(unittest.TestCase):\n'
                                           '    def test_total(self):\n        self.assertEqual(total(3), 6)\n')
        (self.root / 'runner.py').write_text(RUNNER)
        self.scenario = '[unit] price 3 -> total 6'
        (self.plan / 'spec.md').write_text('# Coupon\n\n## Change plan\n\n1. Double price\n'
                                         '   a. app.py - total\n   tests: ' + self.scenario + '\n')
        (self.plan / 'implementation-notes.md').write_text('## Change set 1\n- Tests added: tests.py::test_total\n')
        self.mapping = {'id': 'CS1-S1', 'scenario': self.scenario, 'receipt': 'unit-run.json',
                        'source': 'tests.py', 'case': 'tests.CouponTest::test_total'}
        self.manifest = {'schema': 'dev.test-evidence/1', 'scenarios': [self.mapping]}
        self.write_manifest()

    def write_manifest(self):
        (self.plan / 'test-evidence.json').write_text(json.dumps(self.manifest))

    def capture(self, layer='unit', command=None):
        report = self.root / 'results.xml'
        return subprocess.run([sys.executable, str(CHECK), 'capture', '--layer', layer,
                               '--report', str(report), '--receipt', str(self.plan / 'unit-run.json'),
                               '--source', 'tests.py', '--source', 'app.py', '--',
                               *(command or [sys.executable, 'runner.py', str(report)])],
                              cwd=self.root, capture_output=True, text=True)

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECK), str(self.plan), '--repo-root', str(self.root), *args],
                              capture_output=True, text=True)

    def test_real_passing_runner_case_satisfies_mapping(self):
        captured = self.capture()
        self.assertEqual(captured.returncode, 0, captured.stderr)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('runner evidence verified', result.stdout)

    def test_comment_only_name_cannot_pass_either_gate(self):
        (self.root / 'tests.py').write_text('# test_total\n')
        self.assertEqual(self.check().returncode, 1)
        inventory = self.check('--inventory-only')
        self.assertEqual(inventory.returncode, 1)
        self.assertIn('contains no test named', inventory.stdout)

    def test_inventory_declares_its_limited_assurance(self):
        result = self.check('--inventory-only')
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn('execution and layers NOT verified', result.stdout)

    def test_wrong_layer_rejected(self):
        self.assertEqual(self.capture('integration').returncode, 0)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('layer disagrees', result.stdout)

    def test_changed_implementation_invalidates_result(self):
        self.assertEqual(self.capture().returncode, 0)
        (self.root / 'app.py').write_text('def total(price):\n    return price\n')
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('source changed', result.stdout)

    def test_failed_command_cannot_satisfy_scenario(self):
        (self.root / 'app.py').write_text('def total(price):\n    return price\n')
        self.assertEqual(self.capture().returncode, 1)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('command did not pass', result.stdout)

    def test_skipped_case_cannot_satisfy_scenario(self):
        p = self.root / 'tests.py'
        p.write_text(p.read_text().replace('    def test_total', '    @unittest.skip("not exercised")\n    def test_total'))
        self.assertEqual(self.capture().returncode, 0)
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('absent, failed, or skipped', result.stdout)

    def test_changed_report_is_rejected(self):
        self.assertEqual(self.capture().returncode, 0)
        (self.root / 'results.xml').write_text('<testsuite/>')
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('report changed', result.stdout)

    def test_command_without_fresh_report_removes_old_receipt(self):
        self.assertEqual(self.capture().returncode, 0)
        result = self.capture(command=[sys.executable, '-c', 'pass'])
        self.assertEqual(result.returncode, 1)
        self.assertIn('fresh JUnit report', result.stderr)
        self.assertFalse((self.plan / 'unit-run.json').exists())

    def test_one_runner_case_cannot_count_for_two_scenarios(self):
        self.assertEqual(self.capture().returncode, 0)
        p = self.plan / 'spec.md'
        second = '[unit] price 4 -> total 8'
        p.write_text(p.read_text().replace(self.scenario, self.scenario + '; ' + second))
        self.manifest['scenarios'].append({**self.mapping, 'id': 'CS1-S2', 'scenario': second})
        self.write_manifest()
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('already used', result.stdout)

    def test_reordered_or_missing_scenario_is_rejected(self):
        self.assertEqual(self.capture().returncode, 0)
        self.mapping['scenario'] = '[unit] different promise -> different result'
        self.write_manifest()
        self.assertEqual(self.check().returncode, 1)
        self.manifest['scenarios'] = []
        self.write_manifest()
        self.assertIn('has no scenario mapping', self.check().stdout)

    def test_dynamic_runner_name_needs_no_source_substring(self):
        self.assertEqual(self.capture().returncode, 0)
        # Actual runner-generated parameter titles can differ from source literals.
        (self.plan / 'implementation-notes.md').write_text('## Change set 1\n- Tests added: tests.py::human summary\n')
        self.assertEqual(self.check().returncode, 0)
