"""Runtime provenance and repository readiness: what ran, and whether it can run, before the expensive part."""

import contextlib
import io
import json
import os
import shutil
import sys
from pathlib import Path
from unittest import mock

from runner import cli, events, provenance
from runner.model import Run
from runner.tests.helpers import CONTRACT, SPEC, FactoryTestCase, git, happy_scenario, make_repo, review_step, scope_files
from runner.worker import Worker

GENERATED = provenance.GENERATED


class ProvenanceTestCase(FactoryTestCase):
    def call(self, *argv: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(list(argv))
        return code, stdout.getvalue(), stderr.getvalue()

    def drifted_copy(self, where: Path) -> Path:
        shutil.copytree(GENERATED, where, ignore=shutil.ignore_patterns("__pycache__"))
        skill = where / "skills" / "build" / "SKILL.md"
        skill.write_text(skill.read_text() + "\nAn older instruction the checkout no longer has.\n")
        return where

    def repo_with(self, contract: dict, name: str = "provenance") -> Path:
        repo = make_repo(self.root, name)
        (repo / ".factory" / "contract.json").write_text(json.dumps(contract))
        git(repo, "commit", "--quiet", "-am", "contract")
        git(repo, "push", "--quiet")
        return repo


class RuntimeManifestTests(ProvenanceTestCase):
    def test_every_attempt_records_its_effective_runtime_without_secret_values(self):
        contract = {**CONTRACT, "environment": {"required": ["FACTORY_TEST_API_KEY"]}}
        repo = self.repo_with(contract)
        with mock.patch.dict(os.environ, {"FACTORY_TEST_API_KEY": "sk_live_do_not_record"}):
            run = self.queued_run(repo=repo)
            data = self.work(run)
        self.assertEqual(data["status"], "done", data["human"])
        manifest_text = (run.attempt_dir("build", 1) / "runtime.json").read_text()
        self.assertNotIn("sk_live_do_not_record", manifest_text)
        manifest = json.loads(manifest_text)
        self.assertEqual(manifest["schema"], "factory.runtime/1")
        self.assertEqual((manifest["model"], manifest["effort"]), ("openai.gpt-5.6-luna", "medium"))
        self.assertEqual(manifest["hosts"]["codex"], "codex-cli 0.0.0-stub")
        self.assertEqual(manifest["skills"]["skills_id"], f"sha256:{provenance.tree_sha256(GENERATED)}")
        self.assertTrue(manifest["skills"]["installed_matches_generated"])
        self.assertEqual(len(manifest["runner"]["content_sha256"]), 64)
        self.assertIn("uncommitted_changes", manifest["runner"])
        self.assertEqual(manifest["contract_sha256"], data["intent"] and
                         json.loads((run.dir / "intent" / "approved.json").read_text())["contract_sha256"])
        self.assertEqual(manifest["environment"]["required"], {"FACTORY_TEST_API_KEY": "set"})
        self.assertEqual(manifest["environment"]["github_token"], "forwarded")
        self.assertEqual(manifest["config"]["semantic"], {"max_retries": 5, "stop_on_repeated_reason": False})
        build = [a for a in data["attempts"] if a["stage"] == "build"][0]
        self.assertEqual(build["runtime"]["skills_id"], manifest["skills"]["skills_id"])

    def work(self, run: Run) -> dict:
        Worker(self.home, run.id, grace=1).run()
        return json.loads((run.dir / "run.json").read_text())

    def test_an_uncommitted_runner_edit_changes_the_runner_identity(self):
        copy = self.root / "factory-copy"
        shutil.copytree(provenance.FACTORY_ROOT, copy, ignore=shutil.ignore_patterns("__pycache__", "tests"))
        with mock.patch.object(provenance, "FACTORY_ROOT", copy):
            before = provenance.runner_identity()["content_sha256"]
            gate = copy / "runner" / "gates.py"
            gate.write_text(gate.read_text() + "\n# an uncommitted local edit\n")
            after = provenance.runner_identity()["content_sha256"]
        self.assertNotEqual(before, after)


class SkillDriftTests(ProvenanceTestCase):
    def test_a_stale_codex_plugin_cache_falls_back_to_the_generated_skills_and_continues(self):
        cache = Path(os.environ["CODEX_HOME"]) / "plugins" / "cache" / "nurbot" / "factory" / "0.1.0"
        self.drifted_copy(cache)
        self.scenario(happy_scenario())
        run = self.queued_run()
        Worker(self.home, run.id, grace=1).run()
        data = json.loads((run.dir / "run.json").read_text())
        self.assertEqual(data["status"], "done", data["human"])
        attempt = data["attempts"][0]
        self.assertEqual(attempt["skills"]["resolution"], "direct-path")
        self.assertIn(str(cache), attempt["skills"]["fallback"])
        prompt = (run.attempt_dir("scope-review", 1) / "prompt.txt").read_text()
        self.assertTrue(prompt.startswith(f"Follow the skill at {GENERATED / 'skills' / 'scope-review' / 'SKILL.md'}."))
        self.assertNotIn("$factory:", prompt)
        fallback = [e for e in events.read(run.dir) if e["event"] == "skills.fallback"]
        self.assertIn("reads the generated skills", fallback[0]["data"]["reason"])
        runtime = json.loads((run.attempt_dir("scope-review", 1) / "runtime.json").read_text())
        self.assertEqual(runtime["skills"]["installed"]["path"], str(cache))
        self.assertFalse(runtime["skills"]["installed_matches_generated"])

    def test_preflight_allows_drift_and_doctor_warns(self):
        with mock.patch.dict(os.environ, {"FACTORY_STUB_PLUGIN_SOURCE": str(self.drifted_copy(self.root / "old"))}):
            code, out, _ = self.call("doctor")
            self.assertIn(f"[warn] codex:plugin: the installed Codex factory plugin ({self.root / 'old'}) differs from "
                          "the generated skills; attempts read the generated skills by path instead", out)
            self.assertNotIn("[fail] codex:plugin", out)

    def test_source_and_installed_bundles_never_share_a_label(self):
        drifted = self.drifted_copy(self.root / "old")
        listing = f"factory@nurbot  installed, enabled  0.1.0  {drifted}\n"
        installed = provenance.skill_bundles(listing)
        with mock.patch.dict(os.environ, {"FACTORY_DIRECT_SKILL_PATH": "1"}):
            direct = provenance.skill_bundles(listing)
        self.assertEqual((installed["resolution"], direct["resolution"]), ("direct-path", "direct-path"))
        self.assertIn("differs from the generated skills", installed["fallback"])
        self.assertIsNone(direct["fallback"])
        self.assertEqual(direct["skills_id"], f"sha256:{provenance.tree_sha256(GENERATED)}")
        self.assertEqual(installed["skills_id"], direct["skills_id"])
        self.assertNotEqual(installed["installed"]["sha256"], installed["generated"]["sha256"])
        self.assertEqual(provenance.skill_bundles("")["fallback"], "the Codex factory plugin is not installed")


class ConfigBoundaryTests(ProvenanceTestCase):
    def test_a_configuration_change_applies_from_the_next_attempt_and_is_recorded(self):
        scenario = happy_scenario()
        scenario["scope-review"] = [{**review_step(), "sleep": 2}]
        self.scenario(scenario)
        run = self.queued_run()
        self.assertEqual(self.call("resume", run.id, "--detach")[0], 0)
        self.wait_for(lambda: "process.spawned" in [e["event"] for e in events.read(run.dir)], 20, "spawn")
        (self.home / "config.json").write_text(json.dumps({**self.fast_config, "stop_on_repeated_reason": True}))
        data = self.wait_status(run.dir, ("done", "needs-human", "cancelled"))
        review, build = data["attempts"][0], data["attempts"][1]
        self.assertEqual(review["outcome"], "done", "the running attempt keeps its initial semantic settings")
        self.assertEqual(review["config"]["semantic"]["stop_on_repeated_reason"], False)
        self.assertEqual(review["config"]["semantic"]["max_retries"], 5)
        self.assertEqual(build["config"]["semantic"]["stop_on_repeated_reason"], True)
        self.assertEqual(build["config"]["semantic"]["max_retries"], 5)
        self.assertEqual((data["status"], data["stage"]), ("done", "ship"))
        changed = [e for e in events.read(run.dir) if e["event"] == "config.changed"]
        self.assertEqual(len(changed), 1)
        self.assertEqual((changed[0]["stage"], changed[0]["data"]["previous"]), ("build", "scope-review-1"))

    def test_existing_configuration_stays_readable(self):
        legacy = {"schema": 1, "max_concurrent_stages": 2, "notify": False, "max_tokens_per_run": 20_000_000,
                  "stop_on_repeated_reason": True, "stage_poll_seconds": 5, "heartbeat_seconds": 10,
                  "repos": {str(self.root): {"codex_sandbox": "bypass"}}}
        (self.home / "config.json").write_text(json.dumps(legacy))
        from runner import config
        effective = provenance.effective_config(config.load(self.home), str(self.root))
        self.assertEqual(effective["values"]["codex_sandbox"], "bypass")
        self.assertEqual(effective["semantic"], {"max_retries": 5, "stop_on_repeated_reason": True})


class ReadinessTests(ProvenanceTestCase):
    def test_an_unset_required_environment_name_stops_the_handoff(self):
        repo = self.repo_with({**CONTRACT, "environment": {"required": ["FACTORY_TEST_NOT_SET"]}}, "envgap")
        self.claude([scope_files(SPEC)])
        code, out, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("required environment variable(s) unset: FACTORY_TEST_NOT_SET", out)
        run = Run.load(next((self.home / "runs").glob("*-webhook")))
        self.assertEqual(run.status, "scoping")
        self.assertEqual([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"], [])

    def test_a_plan_the_contract_cannot_launch_is_refused_at_handoff(self):
        contract = {key: value for key, value in CONTRACT.items() if key != "e2e"}
        repo = self.repo_with({**contract, "e2e": {"none": "a library without an application"}}, "nolaunch")
        self.claude([scope_files(SPEC)])
        code, out, _ = self.call("new", str(repo), "Add idempotency", "--plan", "webhook", "--yes", "--detach")
        self.assertEqual(code, 1)
        self.assertIn("the plan has [e2e] scenarios but the contract says there is no e2e driver (a library without "
                      "an application)", out)
        self.assertEqual(Run.load(next((self.home / "runs").glob("*-webhook"))).status, "scoping")
        self.assertEqual([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"], [])

    def test_doctor_repo_checks_contract_environment_commands_and_worktree_git(self):
        contract = {**CONTRACT,
                    "setup": [{"id": "deps", "run": ["sh", "-c", "echo installed"]},
                              {"id": "broken", "run": ["sh", "-c", "echo missing lockfile >&2; exit 3"]}],
                    "validation": [{"id": "readme", "run": ["test", "-f", "README.md"]},
                                   {"id": "lint", "script": "scripts/lint.sh"},
                                   {"id": "tool", "run": ["factory-no-such-tool"], "env": ["FACTORY_TEST_NOT_SET"]}]}
        repo = self.repo_with(contract, "doctored")
        code, out, _ = self.call("doctor", "--repo", str(repo), "--run-setup", "-v")
        self.assertEqual(code, 1)
        for line in ("[ok] repo:contract: 3 validation command(s); CI requires ci",
                     "[fail] repo:environment: unset: FACTORY_TEST_NOT_SET",
                     "[fail] repo:command:lint: scripts/lint.sh does not exist",
                     "[fail] repo:command:tool: factory-no-such-tool not found on PATH",
                     "[ok] repo:gh:token-forwarding: a GitHub token reaches sandboxed attempts",
                     "[ok] repo:worktree-git: a linked worktree commits under the narrowed sandbox grants"
                     if sys.platform == "darwin" or shutil.which("bwrap") else
                     "[warn] repo:worktree-git: sandbox enforcement unavailable",
                     "[ok] repo:setup:deps: sh -c 'echo installed' exited 0 in the host boundary",
                     "[fail] repo:setup:broken: sh -c 'echo missing lockfile >&2; exit 3' exited 3 in the host "
                     "boundary: missing lockfile"):
            self.assertIn(line, out)
        self.assertEqual(git(repo, "worktree", "list").count("\n"), 0)
        self.assertNotIn("factory-doctor", git(repo, "branch", "--list"))
        self.assertFalse(any((self.home / "doctor").iterdir()) if (self.home / "doctor").exists() else False)

    def test_doctor_repo_rejects_an_incomplete_contract(self):
        repo = self.repo_with({"schema": "factory.repo-contract/1", "validation": [{"id": "x", "run": ["true"]}]},
                              "incomplete")
        code, out, _ = self.call("doctor", "--repo", str(repo))
        self.assertEqual(code, 1)
        self.assertIn("[fail] repo:contract: contract.json is malformed: contract: missing 'ci'", out)

    def test_doctor_repo_warns_about_tracked_plan_files_without_failing(self):
        repo = self.repo_with(CONTRACT, "tracked-plans")
        (repo / ".dev" / "old").mkdir(parents=True)
        (repo / ".dev" / "old" / "spec.md").write_text("# old\n")
        git(repo, "add", "-f", ".dev/old/spec.md")
        git(repo, "commit", "--quiet", "-m", "tracked plan")
        git(repo, "push", "--quiet")
        code, out, _ = self.call("doctor", "--repo", str(repo))
        self.assertIn("[warn] repo:plans: the base tracks plan files under .dev/: .dev/old/spec.md; runs leave them "
                      "alone", out)
        self.assertNotIn("[fail] repo:plans", out)

    def test_smoke_resolves_skills_once_and_is_cached(self):
        code, out, _ = self.call("doctor", "--smoke", "-v")
        self.assertIn("[ok] codex:smoke:openai.gpt-5.6-sol: answered 'FACTORY-SMOKE-OK scope-review'", out)
        execs = len([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"])
        self.assertEqual(execs, 2)
        code, out, _ = self.call("doctor", "--smoke", "-v")
        self.assertIn("[ok] codex:smoke:openai.gpt-5.6-luna: cached from", out)
        self.assertIn("[ok] codex:model:build: openai.gpt-5.6-luna answered a cached smoke check", out)
        self.assertEqual(len([c for c in self.stub_calls("codex") if c["argv"][0] == "exec"]), execs)
        (self.home / "capabilities.json").unlink()
        with mock.patch.dict(os.environ, {"FACTORY_STUB_SMOKE_ANSWER": "I cannot find that skill"}):
            code, out, _ = self.call("doctor", "--smoke")
        self.assertEqual(code, 1)
        self.assertIn("[fail] codex:smoke:openai.gpt-5.6-sol: exit 0: I cannot find that skill", out)
