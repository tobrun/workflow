"""Unit tests for the workflow's memory of its own runs: the friction signals and
journal entry skill-metrics.py writes at the end of a run.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test builds a scratch git repository, a fake Claude Code transcript, and a
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
    # The start call has no result yet: it is the command running when `start` looks.
    line("assistant", "2026-01-05T10:00:02.000Z", tool_use("t0", "python3 /p/scripts/skill-metrics.py start build")),
    line("assistant", "2026-01-05T10:00:02.600Z", tool_use("t1", "python3 lint-spec.py .dev/x/spec.md")),
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


HARNESS_VARS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "OPENCODE_SESSION_ID", "PI_SESSION_FILE",
                "PI_SESSION_ID", "CODEX_HOME", "XDG_DATA_HOME")


def clean_env() -> dict:
    """The test process's environment without the harness hosting the test run."""
    return {k: v for k, v in os.environ.items() if k not in HARNESS_VARS}


class Sandbox:
    """A scratch repo with a transcript where skill-metrics.py will find it."""

    def __init__(self, tmp: Path, rows: list[dict] = TRANSCRIPT):
        tmp = tmp.resolve()  # git reports the real path; macOS temp dirs sit behind a symlink
        self.repo = tmp / f"repo-{os.getpid()}-{id(self)}"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                        "--allow-empty", "-m", "init"], cwd=self.repo, check=True)
        self.config = tmp / "claude"
        slug = re.sub(r"[^A-Za-z0-9]", "-", str(self.repo))
        self.transcript = self.config / "projects" / slug / "session.jsonl"  # the session id is "session"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        self.memory = tmp / "memory"
        self.env = {**clean_env(), "CLAUDE_CONFIG_DIR": str(self.config), "CLAUDE_CODE_SESSION_ID": "session",
                    "DEV_MEMORY_DIR": str(self.memory)}

    def metrics(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(METRICS), *args], cwd=self.repo, env=self.env,
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



class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Sandbox(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_end_journals_measured_signals_with_their_lines(self):
        entry = self.box.run("the brief left out the Validation block")
        self.assertEqual(entry["skill"], "build")
        self.assertEqual(entry["id"], "r-20260105T100002-build")
        self.assertEqual(entry["counts"], {"change_sets": "2"})
        self.assertEqual(entry["checkers"]["lint-spec.py"]["runs"], 2)
        self.assertEqual(entry["checkers"]["lint-spec.py"]["failed"], 1)
        self.assertEqual(entry["checkers"]["lint-spec.py"]["first_failure"]["line"], 5)
        kinds = {s["type"]: s for s in entry["signals"]}
        self.assertEqual(kinds["denied"]["line"], 9)
        self.assertEqual(kinds["tool_error"]["line"], 11)
        self.assertEqual(kinds["interrupt"]["line"], 12)
        self.assertEqual(kinds["user_turn"]["line"], 13)
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


if __name__ == "__main__":
    unittest.main()
