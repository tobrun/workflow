"""Unit tests for the workflow's memory of its own runs: the friction signals and
journal entry skill-metrics.py writes at the end of a run, and the claims.py
store the reflect skill loops against.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test builds a scratch git repository, a fake Claude transcript, and a
scratch memory store, and runs the scripts as subprocesses, so it proves the
CLI contract the skills rely on.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
METRICS = REPO_ROOT / "dev" / "scripts" / "skill-metrics.py"
CLAIMS = REPO_ROOT / "dev" / "scripts" / "claims.py"


def line(kind: str, stamp: str, content, **extra) -> dict:
    return {"type": kind, "timestamp": stamp, "message": {"role": kind, "content": content}, **extra}


def tool_use(tool_id: str, command: str) -> list[dict]:
    return [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {"command": command}}]


def tool_result(tool_id: str, text: str, error: bool) -> list[dict]:
    return [{"type": "tool_result", "tool_use_id": tool_id, "is_error": error, "content": text}]


TRANSCRIPT = [
    line("user", "2026-01-05T09:00:00.000Z", "an earlier conversation the run must ignore"),
    line("user", "2026-01-05T10:00:00.000Z", "<command-name>/dev:build</command-name>"),
    line("user", "2026-01-05T10:00:01.000Z", "Base directory for this skill: /x", isMeta=True),
    line("assistant", "2026-01-05T10:00:02.000Z", tool_use("t1", "python3 lint-spec.py .dev/x/spec.md")),
    line("user", "2026-01-05T10:00:03.000Z", tool_result("t1", "spec.md:4: missing tests line", True)),
    line("assistant", "2026-01-05T10:00:04.000Z", tool_use("t2", "python3 lint-spec.py .dev/x/spec.md")),
    line("user", "2026-01-05T10:00:05.000Z", tool_result("t2", "clean", False)),
    line("assistant", "2026-01-05T10:00:06.000Z", tool_use("t3", "rm -rf build")),
    line("user", "2026-01-05T10:00:07.000Z",
         tool_result("t3", "The user doesn't want to proceed with this tool use.", True)),
    line("assistant", "2026-01-05T10:00:08.000Z", tool_use("t4", "npm test")),
    line("user", "2026-01-05T10:00:09.000Z", tool_result("t4", "Exit code 1 ENOENT package.json", True)),
    line("user", "2026-01-05T10:00:10.000Z", [{"type": "text", "text": "[Request interrupted by user]"}]),
    line("user", "2026-01-05T10:00:11.000Z", "stop rerunning the whole validation block after every change set"),
    line("assistant", "2026-01-05T10:00:12.000Z", tool_use("t5", "grep -n lint-spec.py README.md")),
    line("user", "2026-01-05T10:00:13.000Z", tool_result("t5", "", False)),
]


class Sandbox:
    """A scratch repo with a transcript where skill-metrics.py will find it."""

    def __init__(self, tmp: Path, rows: list[dict] = TRANSCRIPT):
        self.repo = tmp / f"repo-{os.getpid()}-{id(self)}"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], cwd=self.repo, check=True)
        self.config = tmp / "claude"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        self.transcript = self.config / "projects" / slug / "session.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.memory = tmp / "memory"
        self.env = {**os.environ, "CLAUDE_CONFIG_DIR": str(self.config), "DEV_MEMORY_DIR": str(self.memory)}

    def metrics(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(METRICS), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def claims(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(CLAIMS), *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True)

    def journal(self) -> list[dict]:
        return [json.loads(l) for p in sorted((self.memory / "journal").glob("*.jsonl"))
                for l in p.read_text(encoding="utf-8").splitlines()]

    def run(self, *friction: str) -> dict:
        self.metrics("start", "build")
        args = ["end", "build", "--count", "change_sets=2"]
        for text in friction:
            args += ["--friction", text]
        result = self.metrics(*args)
        assert result.returncode == 0, result.stderr
        return self.journal()[-1]

    def candidates(self, rows: list[dict]) -> Path:
        path = self.repo / "candidates.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return path


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_journals_measured_signals_with_their_lines(self):
        entry = self.box.run("the brief left out the Validation block")
        self.assertEqual(entry["skill"], "build")
        self.assertEqual(entry["id"], "r-20260105T100000-build")
        self.assertEqual(entry["counts"], {"change_sets": "2"})
        self.assertEqual(entry["checkers"]["lint-spec.py"]["runs"], 2)
        self.assertEqual(entry["checkers"]["lint-spec.py"]["failed"], 1)
        self.assertEqual(entry["checkers"]["lint-spec.py"]["first_failure"]["line"], 4)
        kinds = {s["type"]: s for s in entry["signals"]}
        self.assertEqual(kinds["denied"]["line"], 8)
        self.assertEqual(kinds["tool_error"]["line"], 10)
        self.assertEqual(kinds["interrupt"]["line"], 11)
        self.assertEqual(kinds["user_turn"]["line"], 12)
        self.assertEqual(entry["signal_counts"],
                         {"denied": 1, "tool_error": 1, "interrupt": 1, "user_turn": 1})
        self.assertEqual(entry["friction"], ["the brief left out the Validation block"])
        self.assertEqual(entry["ledger_line"], 1)
        self.assertNotIn("earlier conversation", json.dumps(entry))

    def test_metrics_table_reports_signals_and_journal(self):
        self.box.metrics("start", "build")
        out = self.box.metrics("end", "build").stdout
        self.assertIn("lint-spec.py 2 runs / 1 failed", out)
        self.assertIn("journal:", out)
        row = json.loads((self.box.repo / ".dev" / "metrics.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["signal_counts"]["denied"], 1)

    def test_more_than_three_friction_lines_is_refused_before_writing(self):
        self.box.metrics("start", "build")
        result = self.box.metrics("end", "build", *sum((["--friction", f"f{i}"] for i in range(4)), []))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("at most 3", result.stderr)
        self.assertFalse((self.box.repo / ".dev" / "metrics.jsonl").exists())

    def test_journal_can_be_turned_off(self):
        (self.box.repo / ".dev").mkdir()
        (self.box.repo / ".dev" / "config.json").write_text('{"memory": {"enabled": false}}')
        self.box.metrics("start", "build")
        result = self.box.metrics("end", "build")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("journal:", result.stdout)
        self.assertFalse(self.box.memory.exists())


class ClaimsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(Path(self.tmp.name))
        self.entry = self.box.run("the brief left out the Validation block")
        self.run_id = self.entry["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def claim(self, **over) -> dict:
        row = {"skill": "build", "thread": "build-validation-reruns", "kind": "friction",
               "claim": "The user had to stop build from rerunning the full Validation block.",
               "quote": "stop rerunning the whole validation block",
               "source": {"run": self.run_id, "line": 12}, "salience": 2}
        row.update(over)
        return row

    def add(self, *rows: dict) -> subprocess.CompletedProcess:
        return self.box.claims("add", str(self.box.candidates(list(rows))))

    def stored(self) -> list[dict]:
        return [json.loads(l) for l in (self.box.memory / "claims.jsonl").read_text().splitlines()]

    def test_pending_then_mark(self):
        pending = json.loads(self.box.claims("pending").stdout)
        self.assertEqual([r["id"] for r in pending], [self.run_id])
        self.assertEqual(self.box.claims("mark", self.run_id).returncode, 0)
        self.assertEqual(json.loads(self.box.claims("pending").stdout), [])
        self.assertNotEqual(self.box.claims("mark", "r-nope").returncode, 0)

    def test_quote_found_at_cited_line_is_added_and_derived(self):
        result = self.add(self.claim())
        self.assertEqual(result.returncode, 0, result.stdout)
        [row] = self.stored()
        self.assertEqual(row["id"], "c-0001")
        self.assertEqual(row["observed_at"], "2026-01-05T10:00:00.000Z")
        self.assertIn("[c-0001]", (self.box.memory / "skills" / "build.md").read_text())
        self.assertIn("build-validation-reruns", (self.box.memory / "threads.yaml").read_text())
        self.assertEqual(self.box.claims("check").returncode, 0)

    def test_journal_quote_without_line_is_checked_against_the_entry(self):
        self.assertEqual(self.add(self.claim(quote="the brief left out the Validation block",
                                             source={"run": self.run_id})).returncode, 0)

    def test_batch_is_all_or_nothing(self):
        bad = self.claim(quote="words the user never typed", thread="other-thread")
        result = self.add(self.claim(), bad)
        self.assertEqual(result.returncode, 1)
        self.assertIn("candidate 2: quote not found verbatim", result.stdout)
        self.assertFalse((self.box.memory / "claims.jsonl").exists())

    def test_shape_and_source_errors(self):
        cases = {
            "source.run 'r-missing' is not in the journal": self.claim(source={"run": "r-missing", "line": 12}),
            "kind must be one of": self.claim(kind="vibe"),
            "credential shape": self.claim(claim="Leaked token=abcdefghijklmnop in the run log."),
            "has no readable line": self.claim(source={"run": self.run_id, "line": 999}),
            "subagents": self.claim(source={"run": self.run_id, "line": 1, "transcript": "/etc/passwd"}),
            "em dash": self.claim(claim="Build reran the block \u2014 twice in one wave."),
        }
        for message, row in cases.items():
            with self.subTest(message):
                result = self.add(row)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stdout)

    def test_supersede_and_duplicate(self):
        self.add(self.claim())
        self.assertIn("duplicates c-0001", self.add(self.claim()).stdout)
        newer = self.claim(quote="the brief left out the Validation block", source={"run": self.run_id},
                           supersedes="c-0001")
        self.assertEqual(self.add(newer).returncode, 0)
        self.assertEqual([r["status"] for r in self.stored()], ["superseded", "active"])

    def test_retract_forgets_and_blocks_readding(self):
        self.add(self.claim())
        self.assertEqual(self.box.claims("retract", "c-0001", "--reason", "an interview answer").returncode, 0)
        self.assertFalse((self.box.memory / "skills" / "build.md").exists())
        self.assertNotIn("c-0001", (self.box.memory / "threads.yaml").read_text())
        self.assertIn("was retracted as c-0001", self.add(self.claim()).stdout)

    def test_resolution_reopens_on_a_later_claim(self):
        self.add(self.claim())
        self.assertEqual(self.box.claims("resolve", "build-validation-reruns", "--commit", "abc1234",
                                         "--eval", "build-parallel-wave").returncode, 0)
        self.assertIn("| build-validation-reruns |", (self.box.memory / "progression.md").read_text())
        self.assertIn("no open threads", self.box.claims("top").stdout)
        later = [dict(r, timestamp=r["timestamp"].replace("2026-01-05", "2030-01-01")) for r in TRANSCRIPT]
        self.box.transcript.write_text("".join(json.dumps(r) + "\n" for r in TRANSCRIPT + later))
        entry = self.box.run()
        self.assertEqual(self.add(self.claim(source={"run": entry["id"], "line": len(TRANSCRIPT) + 12})).returncode, 0)
        top = self.box.claims("top").stdout
        self.assertIn("| build-validation-reruns | build | reopened |", top)

    def test_check_flags_hand_edited_derived_files(self):
        self.add(self.claim())
        (self.box.memory / "skills" / "build.md").write_text("edited by hand\n")
        result = self.box.claims("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("skills/build.md: stale", result.stdout)

    def test_show_and_report(self):
        self.add(self.claim())
        shown = self.box.claims("show", "c-0001").stdout
        self.assertIn('quote: "stop rerunning the whole validation block"', shown)
        self.assertIn("context (session.jsonl:12)", shown)
        out = self.box.repo / "report.html"
        self.assertEqual(self.box.claims("report", str(out)).returncode, 0)
        self.assertIn("build-validation-reruns", out.read_text())


if __name__ == "__main__":
    unittest.main()
