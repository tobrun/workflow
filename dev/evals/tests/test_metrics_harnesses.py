"""Unit tests for skill-metrics.py reading each harness's own session store.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -t .
Each test writes a small session in the shape Codex, opencode, or Pi stores it
(shapes copied from real sessions of codex-cli 0.163, opencode 2.0, and pi
0.87), points the harness's own variable at it, and runs the script as a
subprocess, so it proves the run is measured where the skill actually ran.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dev.evals.tests.test_memory import METRICS, clean_env

START = "python3 /p/scripts/skill-metrics.py start build"


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


class Box:
    def __init__(self, tmp: Path, git: bool = True, commit: bool = True):
        self.tmp = tmp.resolve()
        self.repo = self.tmp / f"repo-{os.getpid()}-{id(self)}"  # the start snapshot is kept per repo name
        self.repo.mkdir()
        if git:
            subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        if git and commit:
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty",
                            "-m", "init"], cwd=self.repo, check=True)
        self.memory = self.tmp / "memory"
        self.env = {**clean_env(), "DEV_MEMORY_DIR": str(self.memory), "CLAUDE_CONFIG_DIR": str(self.tmp / "claude")}

    def metrics(self, *args: str) -> subprocess.CompletedProcess:
        result = subprocess.run([sys.executable, str(METRICS), *args], cwd=self.repo, env=self.env,
                                capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        return result

    def run(self) -> tuple[str, dict, dict]:
        started = self.metrics("start", "build").stdout
        out = self.metrics("end", "build").stdout
        row = json.loads((self.repo / ".dev" / "metrics.jsonl").read_text().splitlines()[-1])
        journal = [json.loads(l) for p in sorted((self.memory / "journal").glob("*.jsonl"))
                   for l in p.read_text().splitlines()]
        return started + out, row, journal[-1]


# --- Codex --------------------------------------------------------------------

def codex_meta(thread: str, stamp: str, parent: str | None = None) -> dict:
    payload = {"id": thread, "session_id": thread, "timestamp": stamp, "cwd": "/r", "source": "cli"}
    if parent:
        payload.update(parent_thread_id=parent, source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}})
    return {"timestamp": stamp, "type": "session_meta", "payload": payload}


def codex_usage(stamp: str, response: str, inp: int, cached: int, out: int) -> dict:
    return {"timestamp": stamp, "type": "token_usage_record", "payload": {"response_id": response, "usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "cache_write_input_tokens": 0,
        "output_tokens": out, "reasoning_output_tokens": 5, "total_tokens": inp + out}}}


def codex_call(stamp: str, call: str, cmd: str) -> dict:
    return {"timestamp": stamp, "type": "response_item", "payload": {
        "type": "function_call", "name": "exec_command", "call_id": call, "arguments": json.dumps({"cmd": cmd})}}


def codex_output(stamp: str, call: str, code: int) -> dict:
    return {"timestamp": stamp, "type": "response_item", "payload": {
        "type": "function_call_output", "call_id": call, "output": f"Process exited with code {code}\nOutput:\n"}}


class CodexTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Box(Path(self.tmp.name))
        home = self.box.tmp / "codex"
        day = home / "sessions" / "2026" / "01" / "05"
        main, child = "01a00000-0000-7000-8000-000000000001", "01a00000-0000-7000-8000-000000000002"
        write_jsonl(day / f"rollout-2026-01-05T10-00-00-{main}.jsonl", [
            codex_meta(main, "2026-01-05T10:00:00.000Z"),
            codex_usage("2026-01-05T10:00:01.000Z", "resp-before", 999, 0, 999),
            {"timestamp": "2026-01-05T10:00:01.500Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "/build"}},
            codex_call("2026-01-05T10:00:02.000Z", "c0", START),
            codex_usage("2026-01-05T10:00:02.100Z", "resp-1", 1000, 600, 50),
            codex_call("2026-01-05T10:00:03.000Z", "c1", "python3 check-tests.py .dev/x"),
            codex_usage("2026-01-05T10:00:03.100Z", "resp-2", 2000, 1500, 70),
            codex_output("2026-01-05T10:00:03.200Z", "c1", 1),
            codex_call("2026-01-05T10:00:04.000Z", "c2", "npm test"),
            codex_output("2026-01-05T10:00:04.200Z", "c2", 2),
            {"timestamp": "2026-01-05T10:00:05.000Z", "type": "event_msg",
             "payload": {"type": "turn_aborted", "reason": "interrupted"}},
            {"timestamp": "2026-01-05T10:00:06.000Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": "only rerun the failing change set"}},
        ])
        write_jsonl(day / f"rollout-2026-01-05T10-00-03-{child}.jsonl", [
            codex_meta(child, "2026-01-05T10:00:03.500Z", parent=main),
            codex_usage("2026-01-05T10:00:03.600Z", "resp-c1", 300, 0, 20),
        ])
        self.box.env.update(CODEX_HOME=str(home), CODEX_THREAD_ID=main)

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_agents_and_signals_come_from_the_rollouts(self):
        out, row, entry = self.box.run()
        self.assertEqual(row["harness"], "codex")
        self.assertEqual(row["started_at"], "2026-01-05T10:00:02.000Z")
        # input_tokens include the cached share; the meter splits it out and skips responses before the start call.
        self.assertEqual(row["tokens_main"], {"input": 900, "cache_read": 2100, "cache_write": 0, "output": 120,
                                              "reasoning": 10})
        self.assertEqual(row["agents"], 1)
        self.assertEqual(row["tokens_total"], 900 + 2100 + 120 + 320)
        self.assertEqual(row["tools"], {"exec_command": 3})
        self.assertEqual(entry["checkers"]["check-tests.py"], {"runs": 1, "failed": 1,
                                                               "first_failure": {"transcript": entry["transcript"],
                                                                                 "line": 7}})
        self.assertEqual(entry["signal_counts"], {"tool_error": 1, "interrupt": 1, "user_turn": 1})
        self.assertEqual(len(entry["subagent_transcripts"]), 1)
        self.assertIn("codex (session 01a00000", out)
        self.assertIn("in 900 / cache read 2.1k / cache write 0 / out 120 (reasoning 10)", out)

    def test_claims_may_cite_a_subagent_rollout(self):
        # Codex keeps a subagent's rollout beside its parent's, not under it, so the journal lists it.
        _, _, entry = self.box.run()
        [child] = entry["subagent_transcripts"]
        candidate = self.box.tmp / "candidates.jsonl"
        write_jsonl(candidate, [{"skill": "build", "thread": "subagent-spawn", "kind": "friction",
                                 "claim": "The build subagent names the parent thread that spawned it.",
                                 "quote": "01a00000-0000-7000-8000-000000000001", "source": {"run": entry["id"], "line": 0,
                                                                     "transcript": child}, "salience": 1}])
        result = subprocess.run([sys.executable, str(METRICS.with_name("claims.py")), "add", str(candidate)],
                                cwd=self.box.repo, env=self.box.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)


# --- opencode -----------------------------------------------------------------

OPENCODE_SCHEMA = """
create table session_v2 (id text primary key, project_id text, parent_id text, directory text,
                         time_created integer not null);
create table session_message (id text primary key, session_id text not null, type text not null,
                              seq integer not null, time_created integer not null, data text not null);
"""


def oc_assistant(tokens: tuple[int, int, int, int], tools: list[dict], error: dict | None = None) -> dict:
    inp, out, reasoning, read = tokens
    data = {"agent": "build", "content": tools,
            "tokens": {"input": inp, "output": out, "reasoning": reasoning, "cache": {"read": read, "write": 0}}}
    if error:
        data["error"] = error
    return data


def oc_tool(call: str, command: str, exit_code: int = 0, status: str = "completed") -> dict:
    return {"type": "tool", "id": call, "name": "shell", "state": {
        "status": status, "input": {"command": command}, "content": [{"type": "text", "text": "out"}],
        "metadata": {"exit": exit_code}}}


class OpenCodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Box(Path(self.tmp.name))
        data = self.box.tmp / "data"
        (data / "opencode").mkdir(parents=True)
        db = sqlite3.connect(data / "opencode" / "opencode.db")
        db.executescript(OPENCODE_SCHEMA)
        t0 = 1767607200000  # 2026-01-05T10:00:00Z
        db.executemany("insert into session_v2 values (?, 'p', ?, '/r', ?)",
                       [("ses_main", None, t0), ("ses_child", "ses_main", t0 + 4000), ("ses_old", "ses_main", t0 - 1)])
        rows = [
            ("ses_main", "user", 0, t0, {"text": "@build"}),
            ("ses_main", "assistant", 1, t0 + 1000, oc_assistant((5000, 1, 0, 0), [])),
            ("ses_main", "assistant", 2, t0 + 2000, oc_assistant((100, 10, 4, 900), [oc_tool("t0", START, status="running")])),
            ("ses_main", "assistant", 3, t0 + 3000,
             oc_assistant((200, 20, 6, 1800), [oc_tool("t1", "python3 lint-spec.py .dev/x/spec.md", 1),
                                              oc_tool("t2", "rm -rf build", status="error")])),
            ("ses_main", "user", 4, t0 + 5000, {"text": "keep the build directory"}),
            ("ses_main", "assistant", 5, t0 + 6000, oc_assistant((0, 0, 0, 0), [], {"type": "aborted"})),
            ("ses_child", "assistant", 0, t0 + 4500, oc_assistant((40, 4, 0, 0), [])),
            ("ses_old", "assistant", 0, t0, oc_assistant((7777, 0, 0, 0), [])),
        ]
        db.executemany("insert into session_message values (?, ?, ?, ?, ?, ?)",
                       [(f"m{i}", s, t, q, c, json.dumps(d)) for i, (s, t, q, c, d) in enumerate(rows)])
        db.commit()
        db.close()
        self.box.env.update(XDG_DATA_HOME=str(data), OPENCODE_SESSION_ID="ses_main")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_agents_and_signals_come_from_the_database(self):
        _, row, entry = self.box.run()
        self.assertEqual(row["harness"], "opencode")
        self.assertEqual(row["tokens_main"], {"input": 300, "cache_read": 2700, "cache_write": 0, "output": 40,
                                              "reasoning": 10})
        self.assertEqual(row["agents"], 1)  # the child started before the run is not this run's agent
        self.assertEqual(row["tokens_total"], 300 + 2700 + 40 + 44)
        self.assertEqual(entry["checkers"]["lint-spec.py"]["runs"], 1)
        self.assertEqual(entry["signal_counts"], {"tool_error": 1, "user_turn": 1, "interrupt": 1})

    def test_signals_cite_lines_of_the_exported_run(self):
        _, _, entry = self.box.run()
        export = Path(entry["transcript"])
        self.assertEqual(export, self.box.memory / "transcripts" / f"{entry['id']}.jsonl")
        lines = export.read_text().splitlines()
        turn = next(s for s in entry["signals"] if s["type"] == "user_turn")
        self.assertIn("keep the build directory", lines[turn["line"]])
        self.assertEqual(json.loads(lines[0])["seq"], 2)  # the slice opens at the start call
        [child] = entry["subagent_transcripts"]
        self.assertTrue(Path(child).is_file())


# --- Pi -----------------------------------------------------------------------

def pi_message(stamp: str, message: dict) -> dict:
    return {"type": "message", "id": stamp[-6:], "timestamp": stamp, "message": message}


def pi_assistant(stamp: str, inp: int, read: int, out: int, calls: list[tuple[str, str]], stop: str = "toolUse"):
    return pi_message(stamp, {"role": "assistant", "stopReason": stop,
                              "content": [{"type": "toolCall", "id": c, "name": "bash", "arguments": {"command": cmd}}
                                          for c, cmd in calls],
                              "usage": {"input": inp, "output": out, "cacheRead": read, "cacheWrite": 0}})


class PiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box = Box(Path(self.tmp.name))
        self.session = write_jsonl(self.box.tmp / "pi" / "session.jsonl", [
            {"type": "session", "version": 3, "id": "pi-1", "timestamp": "2026-01-05T10:00:00.000Z", "cwd": "/r"},
            pi_message("2026-01-05T10:00:01.000Z", {"role": "user", "content": [{"type": "text", "text": "/skill:build"}]}),
            pi_assistant("2026-01-05T10:00:02.000Z", 100, 500, 10, [("p0", START)]),
            pi_assistant("2026-01-05T10:00:03.000Z", 50, 700, 20, [("p1", "cat missing")]),
            pi_message("2026-01-05T10:00:03.500Z", {"role": "toolResult", "toolCallId": "p1", "isError": True,
                                                    "content": [{"type": "text", "text": "ENOENT"}]}),
            pi_assistant("2026-01-05T10:00:04.000Z", 0, 0, 0, [], stop="aborted"),
        ])
        self.box.env.update(PI_SESSION_FILE=str(self.session), PI_SESSION_ID="pi-1")

    def tearDown(self):
        self.tmp.cleanup()

    def test_tokens_from_the_session_and_agents_are_not_available(self):
        out, row, entry = self.box.run()
        self.assertEqual(row["harness"], "pi")
        self.assertEqual(row["tokens_main"], {"input": 150, "cache_read": 1200, "cache_write": 0, "output": 30})
        self.assertIsNone(row["agents"])
        self.assertIsNone(row["tokens_agents"])
        self.assertEqual(entry["signal_counts"], {"tool_error": 1, "interrupt": 1})
        self.assertEqual(entry["unmeasured_signals"], ["denied"])
        self.assertIn("n/a on pi (its panel runs with --no-session)", out)
        self.assertIn("denied n/a on pi", out)
        self.assertIn("(orchestrator only)", out)

    def test_newest_start_call_wins_when_harness_variables_leak(self):
        # An outer Claude Code session launched this Pi process and ran its own start call earlier.
        claude = self.box.tmp / "claude" / "projects" / "outer" / "outer.jsonl"
        write_jsonl(claude, [{"type": "assistant", "timestamp": "2026-01-05T09:00:00.000Z", "message": {
            "id": "m1", "usage": {"input_tokens": 9, "output_tokens": 9},
            "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": START}}]}}])
        self.box.env["CLAUDE_CODE_SESSION_ID"] = "outer"
        _, row, _ = self.box.run()
        self.assertEqual(row["harness"], "pi")


# --- no harness, baselines, git -----------------------------------------------

class FallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_harness_prints_na_not_zero_and_is_not_a_baseline(self):
        box = Box(Path(self.tmp.name))
        first, row, _ = box.run()
        self.assertIsNone(row["harness"])
        self.assertIsNone(row["tokens_total"])
        self.assertIn("| tokens total                    | n/a |", first)
        self.assertIn("no harness session found", first)
        self.assertNotIn("| 0 |", first.split("git since start")[0])
        second, _, _ = box.run()
        self.assertNotIn("vs previous", second)

    def test_zero_token_rows_from_older_runs_are_not_a_baseline(self):
        box = Box(Path(self.tmp.name))
        (box.repo / ".dev").mkdir()
        old = [{"skill": "build", "seconds": 60, "tokens_total": 0}, {"skill": "build", "seconds": 90, "tokens_total": 4000}]
        write_jsonl(box.repo / ".dev" / "metrics.jsonl", old)
        session = write_jsonl(box.tmp / "pi.jsonl", [pi_assistant(f"2026-01-05T10:00:0{i}.000Z", 1000, 0, 0, [("p", START)])
                                                    for i in range(1)])
        box.env["PI_SESSION_FILE"] = str(session)
        out, _, _ = box.run()
        self.assertIn("1 with tokens on record, median 4.0k tokens", out)

    def test_repository_without_a_commit_still_reports_git(self):
        box = Box(Path(self.tmp.name), commit=False)
        box.metrics("start", "build")
        (box.repo / "a.txt").write_text("one\ntwo\n")
        out = box.metrics("end", "build").stdout
        self.assertIn("0 commits, 1 files (+2/-0)", out)
        self.assertNotIn("not a git repo", out)

    def test_an_answered_copy_of_the_start_command_is_not_the_anchor(self):
        # A start call quoted inside an earlier command already has its result; it is not this run.
        box = Box(Path(self.tmp.name))
        write_jsonl(box.tmp / "claude" / "projects" / "p" / "s1.jsonl", [
            {"type": "assistant", "timestamp": "2026-01-05T10:00:00.000Z", "message": {
                "id": "m1", "usage": {"input_tokens": 5000, "output_tokens": 5000},
                "content": [{"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": f"echo '{START}'"}}]}},
            {"type": "user", "timestamp": "2026-01-05T10:00:01.000Z",
             "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}},
        ])
        box.env["CLAUDE_CODE_SESSION_ID"] = "s1"
        out, row, _ = box.run()
        self.assertEqual(row["harness"], "claude")
        self.assertFalse(row["anchored"])
        self.assertIsNone(row["tokens_total"])  # nothing after the session's end at start time
        self.assertIn("the call itself was not found in the session", out)

    def test_outside_git_says_so(self):
        box = Box(Path(self.tmp.name), git=False)
        box.metrics("start", "build")
        self.assertIn("n/a (not a git repo)", box.metrics("end", "build").stdout)


if __name__ == "__main__":
    unittest.main()
