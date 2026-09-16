import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from runner import hosts, pipeline
from runner.model import Run
from runner.tests.helpers import STUBS


class ArgvTests(unittest.TestCase):
    common = dict(prompt="$factory:build\n", model="openai.gpt-5.6-luna", effort="medium",
                  worktree=Path("/runs/r/worktree"),
                  writable=[Path("/runs/r/reports"), Path("/repo/.git/objects"), Path("/repo/.git/worktrees/worktree")],
                  last_message=Path("/runs/r/attempts/build-1/last-message.md"))

    def test_workspace_write(self):
        with mock.patch.dict(os.environ, {"FACTORY_CODEX_BIN": "codex"}):
            argv = hosts.codex_argv(sandbox="workspace-write", **self.common)
        self.assertEqual(argv, [
            "codex", "exec", "$factory:build\n", "-m", "openai.gpt-5.6-luna",
            "-c", 'model_reasoning_effort="medium"',
            "-c", 'approval_policy="never"',
            "-s", "workspace-write",
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", 'shell_environment_policy.inherit="all"',
            "--add-dir", "/runs/r/reports",
            "--add-dir", "/repo/.git/objects",
            "--add-dir", "/repo/.git/worktrees/worktree",
            "-C", "/runs/r/worktree",
            "--skip-git-repo-check",
            "--json",
            "-o", "/runs/r/attempts/build-1/last-message.md",
        ])

    def test_bypass_replaces_sandbox_and_approval_flags(self):
        with mock.patch.dict(os.environ, {"FACTORY_CODEX_BIN": "codex"}):
            argv = hosts.codex_argv(sandbox="bypass", **self.common)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", argv)
        self.assertNotIn("-s", argv)
        self.assertNotIn('approval_policy="never"', argv)
        with self.assertRaises(ValueError):
            hosts.codex_argv(sandbox="read-only", **self.common)

    def test_claude_fresh_and_resume(self):
        with mock.patch.dict(os.environ, {"FACTORY_CLAUDE_BIN": "claude"}):
            fresh = hosts.claude_argv(prompt="/factory:scope Add idempotency", model="fable", effort="medium",
                                      plugin_root=Path("/f"), run_dir=Path("/runs/r"), session_id="abc", resume=False)
            resumed = hosts.claude_argv(prompt="/factory:scope Add idempotency", model="fable", effort="medium",
                                        plugin_root=Path("/f"), run_dir=Path("/runs/r"), session_id="abc", resume=True)
        self.assertEqual(fresh, ["claude", "/factory:scope Add idempotency", "--model", "fable", "--effort", "medium",
                                 "--plugin-dir", "/f", "--add-dir", "/runs/r", "--session-id", "abc"])
        self.assertEqual(resumed, ["claude", "--model", "fable", "--effort", "medium", "--plugin-dir", "/f",
                                   "--add-dir", "/runs/r", "--resume", "abc"])

    def test_github_token_is_forwarded_unless_already_set(self):
        stub = str(STUBS / "gh")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "gh.json"
            state.write_text(json.dumps({"auth": True}))
            base = {"PATH": os.environ["PATH"], "FACTORY_GH_BIN": stub, "FACTORY_GH_STATE": str(state)}
            with mock.patch.dict(os.environ, {"FACTORY_GH_BIN": stub}):
                env = hosts.attempt_env(run_dir=Path("/r"), plan="p", stage="ship", attempt=1, base=base,
                                        forward_github_token=True)
                self.assertEqual(env["GH_TOKEN"], "gho_stubtoken")
                preset = hosts.attempt_env(run_dir=Path("/r"), plan="p", stage="ship", attempt=1,
                                           base={**base, "GITHUB_TOKEN": "mine"}, forward_github_token=True)
                self.assertNotIn("GH_TOKEN", preset)
                state.write_text(json.dumps({"auth": False}))
                logged_out = hosts.attempt_env(run_dir=Path("/r"), plan="p", stage="ship", attempt=1, base=base,
                                               forward_github_token=True)
                self.assertNotIn("GH_TOKEN", logged_out)

    def test_environment_contract(self):
        env = hosts.attempt_env(run_dir=Path("/runs/r"), plan="p", stage="ship", attempt=3, base={"PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin", "FACTORY_RUN_DIR": "/runs/r", "FACTORY_PLAN": "p",
                               "FACTORY_STAGE": "ship", "FACTORY_ATTEMPT": "3"})


class PromptTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.run = Run.create(Path(self._tmp.name), repo="/r", request="x", plan="webhook")

    def tearDown(self):
        self._tmp.cleanup()

    def test_first_attempt_prompt(self):
        prompt = pipeline.PIPELINE["build"].build_prompt(self.run, 1)
        self.assertTrue(prompt.startswith("$factory:build\n"))
        self.assertIn("No human is available in this session. This is attempt 1 of the build stage.", prompt)
        self.assertIn("Write .dev/webhook/build-result.json as your last action.", prompt)
        self.assertNotIn("Attempt 0", prompt)

    def test_retry_carries_reason_and_note(self):
        attempt = self.run.begin_attempt("build", host="codex", model="m", effort="e")
        self.run.finish_attempt(attempt, outcome="blocked", reason="Validation command failed: npm test",
                                retryable=True, source="gate")
        self.run.data["human"]["note"] = "flaky test fixed upstream"
        prompt = pipeline.PIPELINE["build"].build_prompt(self.run, 2)
        self.assertIn("Attempt 1 ended blocked: Validation command failed: npm test.", prompt)
        self.assertIn("Operator note: flaky test fixed upstream.", prompt)

    def test_direct_skill_path_fallback_is_explicit(self):
        with mock.patch.dict(os.environ, {"FACTORY_DIRECT_SKILL_PATH": "1"}):
            prompt = pipeline.PIPELINE["ship"].build_prompt(self.run, 1)
        first = prompt.splitlines()[0]
        self.assertTrue(first.startswith("Follow the skill at "))
        self.assertTrue(first.endswith("plugins/factory/skills/ship/SKILL.md."))
        self.assertTrue(Path(first[len("Follow the skill at "):-1]).is_file())

    def test_run_context_file(self):
        (self.run.worktree).mkdir()
        self.run.data.update({"branch": "factory/webhook", "base": "main", "base_sha": "abc"})
        path = pipeline.write_run_context(self.run, "build", 2, interactive=False)
        data = json.loads(path.read_text())
        self.assertEqual(data["attempt"], 2)
        self.assertEqual(data["scratch_dir"], str(self.run.dir / "scratch" / "build-2"))
        self.assertTrue(Path(data["scratch_dir"]).is_dir())
        self.assertFalse(data["interactive"])

    def test_scope_prompt_points_at_request_file_for_external_sources(self):
        self.assertEqual(pipeline.scope_prompt(self.run, 1), "/factory:scope x")
        self.run.data["source"] = {"kind": "jira", "key": "PROJ-7"}
        self.run.data["request"] = "PROJ-7: Deduplicate webhooks"
        prompt = pipeline.scope_prompt(self.run, 1)
        self.assertEqual(prompt, f"/factory:scope PROJ-7: Deduplicate webhooks (full request: {self.run.dir / 'request.md'})")
        (self.run.worktree).mkdir()
        self.run.data.update({"branch": "b", "base": "main", "base_sha": "abc"})
        data = json.loads(pipeline.write_run_context(self.run, "scope", 1, interactive=True).read_text())
        self.assertEqual(data["request_file"], str(self.run.dir / "request.md"))
        self.assertEqual(data["source"]["key"], "PROJ-7")
        self.assertEqual((self.run.dir / "request.md").read_text(), "x\n")

    def test_stage_defaults(self):
        table = {name: (s.host, s.model, s.effort, s.timeout_s, s.slot_limited) for name, s in pipeline.PIPELINE.items()}
        self.assertEqual(table, {
            "scope": ("claude", "fable", "medium", None, False),
            "scope-review": ("codex", "openai.gpt-5.6-sol", "low", 45 * 60, True),
            "build": ("codex", "openai.gpt-5.6-luna", "medium", 3 * 3600, True),
            "ship": ("codex", "openai.gpt-5.6-luna", "high", 3 * 3600, True),
        })


class StreamTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "stdout.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, *lines: str) -> None:
        self.path.write_text("\n".join(lines))

    def test_thread_tokens_failures_and_unknown(self):
        self.write(
            json.dumps({"type": "thread.started", "thread_id": "t-1"}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}}),
            json.dumps({"type": "brand.new.event", "x": 1}),
            json.dumps({"type": "turn.failed", "error": {"message": "rate limited"}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 50, "output_tokens": 5}}),
            json.dumps(["not", "an", "object"]),
        )
        stream = hosts.parse_codex_stream(self.path)
        self.assertEqual(stream.thread_id, "t-1")
        self.assertEqual(stream.tokens, 175)
        self.assertEqual(stream.failures, ["rate limited"])
        self.assertEqual(stream.unknown, 1)
        self.assertFalse(stream.truncated)

    def test_truncated_final_line(self):
        self.write(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
                   '{"type": "item.compl')
        stream = hosts.parse_codex_stream(self.path)
        self.assertTrue(stream.truncated)
        self.assertEqual(stream.tokens, 2)

    def test_missing_stream(self):
        self.assertEqual(hosts.parse_codex_stream(self.path).tokens, 0)

    def test_render_hides_unknown_unless_raw(self):
        self.write(
            json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "npm test"}}),
            json.dumps({"type": "item.completed", "item": {"type": "command_execution", "exit_code": 1}}),
            json.dumps({"type": "mystery"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "done"}}),
        )
        self.assertEqual(hosts.render_stream(self.path), ["$ npm test", "  exit 1 (completed)", "agent: done"])
        self.assertEqual(len(hosts.render_stream(self.path, raw=True)), 4)


if __name__ == "__main__":
    unittest.main()
