"""CLI regression fixtures for review completeness and shared spec writes."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AGGREGATE = ROOT / 'dev/skills/ship/scripts/aggregate-findings.py'


class ReviewProtocolTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.lenses = self.root / 'lenses'
        self.verifiers = self.root / 'verifiers'
        self.lenses.mkdir()
        self.verifiers.mkdir()

    def write(self, directory, name, value):
        (directory / f'{name}.json').write_text(json.dumps(value))

    def aggregate(self, expected='security'):
        result = subprocess.run([sys.executable, str(AGGREGATE), 'aggregate',
                                 str(self.lenses), str(self.verifiers), '--expected', expected],
                                capture_output=True, text=True)
        return result, json.loads(result.stdout)

    def lens(self, severity=None):
        finding = {'severity': severity, 'file': 'app.py', 'line': 1,
                   'title': 'Invalid payload accepted', 'detail': 'The validation branch accepts missing name.',
                   'scenario': 'payload missing name'}
        return {'verdict': 'BLOCK' if severity == 'BLOCK' else 'CONCERNS' if severity else 'PASS',
                'findings': [finding] if severity else [], 'good': []}

    def test_empty_panel_is_not_a_pass(self):
        result, report = self.aggregate('security,simplify')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(report['failedLenses'], ['security', 'simplify'])

    def test_complete_empty_findings_are_a_pass(self):
        self.write(self.lenses, 'security', self.lens())
        result, report = self.aggregate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(report['complete'])
        self.assertEqual(report['verdict'], 'PASS')

    def test_unknown_severity_cannot_disappear_into_pass(self):
        self.write(self.lenses, 'security', self.lens('high'))
        result, report = self.aggregate()
        self.assertEqual(result.returncode, 1)
        self.assertIn('severity', ' '.join(report['protocolErrors']))
        self.assertEqual(report['verdict'], 'INCOMPLETE')

    def test_parseable_wrong_shape_is_incomplete(self):
        for value in [[], {'verdict': 'PASS'}, {'verdict': 'PASS', 'findings': [42]},
                      {'verdict': 'PASS', 'findings': [], 'good': 'fine'}]:
            with self.subTest(value=value):
                self.write(self.lenses, 'security', value)
                result, report = self.aggregate()
                self.assertEqual(result.returncode, 1)
                self.assertFalse(report['complete'])

    def test_partial_panel_keeps_confirmed_blocker(self):
        self.write(self.lenses, 'security', self.lens('BLOCK'))
        self.write(self.verifiers, 'v1', [{'id': 'f1', 'status': 'CONFIRMED', 'reason': 'Observed the branch.'}])
        result, report = self.aggregate('security,simplify')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['verdict'], 'INCOMPLETE')
        self.assertEqual(report['findingVerdict'], 'BLOCK')
        self.assertEqual(len(report['blockers']), 1)

    def test_missing_verifier_retains_finding_without_approval(self):
        self.write(self.lenses, 'security', self.lens('BLOCK'))
        result, report = self.aggregate()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['unverifiedFindings'], ['f1'])
        self.assertEqual(report['concerns'][0]['verification'], 'UNVERIFIED')

    def test_malformed_duplicate_and_unknown_verdicts_are_rejected(self):
        self.write(self.lenses, 'security', self.lens('BLOCK'))
        for entries in [[{'id': [], 'status': 'CONFIRMED'}],
                        [{'id': 'wrong', 'status': 'REFUTED', 'reason': 'not a case'}],
                        [{'id': 'f1', 'status': 'YES', 'reason': 'wrong enum'}],
                        [{'id': 'f1', 'status': 'CONFIRMED', 'reason': 'holds'},
                         {'id': 'f1', 'status': 'REFUTED', 'reason': 'does not hold'}]]:
            with self.subTest(entries=entries):
                self.write(self.verifiers, 'v1', entries)
                result, report = self.aggregate()
                self.assertEqual(result.returncode, 1)
                self.assertEqual(report['verdict'], 'INCOMPLETE')
                self.assertTrue(report['protocolErrors'])

    def test_refuted_finding_allows_complete_pass(self):
        self.write(self.lenses, 'security', self.lens('BLOCK'))
        self.write(self.verifiers, 'v1', [{'id': 'f1', 'status': 'REFUTED', 'reason': 'Guard rejects it.'}])
        result, report = self.aggregate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report['verdict'], 'PASS')
        self.assertFalse(report['blockers'])

    def test_expected_set_is_required(self):
        self.write(self.lenses, 'security', self.lens())
        result, report = self.aggregate('')
        self.assertEqual(result.returncode, 1)
        self.assertFalse(report['complete'])

