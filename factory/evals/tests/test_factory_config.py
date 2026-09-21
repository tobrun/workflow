"""Unit tests for factory-config.py: the config reader, validator and CLI.

Drives the script as a subprocess in a temporary repository per change set 1's
and change set 2's `tests:` lines in .dev/config-driven-factory/spec.md.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "factory" / "scripts" / "factory-config.py"


def run(repo: Path, *args: str, plugin_root: Path | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(SCRIPT), "--repo-root", str(repo)]
    if plugin_root is not None:
        cmd += ["--plugin-root", str(plugin_root)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True)


def write_raw(repo: Path, text: str) -> None:
    factory = repo / ".factory"
    factory.mkdir(parents=True, exist_ok=True)
    (factory / "config.yaml").write_text(text)


def write_json_doc(repo: Path, doc: dict) -> None:
    """Write a config.yaml from a plain dict via the script's own dump_config,
    so tests build fixtures the same way the CLI would produce them."""
    sys.path.insert(0, str(SCRIPT.parent))
    import importlib.util

    spec = importlib.util.spec_from_file_location("factory_config_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_doc(repo, doc)


def make_skill(repo: Path, rel: str, content: str = "skill body\n") -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


class TempRepoTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()


class CheckValidationTest(TempRepoTestCase):
    def test_duplicate_phase_id(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True},
                    {"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True},
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'a'", result.stdout)

    def test_undeclared_type(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {},
                "phases": [{"id": "a", "type": "missing", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing", result.stdout)

    def test_empty_phase_list(self) -> None:
        write_json_doc(self.repo, {"version": 1, "types": {}, "phases": []})
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty phase list", result.stdout)

    def test_check_executable_outside_roots(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/x/SKILL.md",
                        "checks": [["${repo_root}/../../etc/passwd"]],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("etc/passwd", result.stdout)

    def test_check_as_shell_string(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/x/SKILL.md",
                        "checks": ["lint-spec.py ${plan_dir}"],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'a'", result.stdout)

    def test_check_naming_unsupported_placeholder(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/x/SKILL.md",
                        "checks": [["${home}/bin/x"]],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("${home}", result.stdout)

    def test_check_executable_with_plan_dir(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/x/SKILL.md",
                        "checks": [["${plan_dir}/x"]],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("${plan_dir}", result.stdout)

    def test_missing_skill_file_and_missing_executable_do_not_resolve(self) -> None:
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/nope/SKILL.md",
                        "checks": [["${repo_root}/nope.py"]],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nope/SKILL.md", result.stdout)
        self.assertIn("nope.py", result.stdout)
        self.assertIn("does not resolve", result.stdout)

    def test_reserved_phase_id(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "defaults", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("reserved", result.stdout)

    def test_unsupported_constructs_never_traceback(self) -> None:
        for raw in (
            "version: 1\nphases: &x [1,2]\n",
            "version: 1\nphases: {a: 1}\n",
            "version: 1\nphases: [1,2]\n",
            "version: 1\nphases: |\n  x\n",
        ):
            write_raw(self.repo, raw)
            result = run(self.repo, "check")
            self.assertEqual(result.returncode, 1, raw)
            self.assertNotIn("Traceback", result.stderr)

    def test_requires_with_colon_in_double_quotes(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {"requires": "Verdict: APPROVED must appear"}},
                "phases": [{"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 0)
        result = run(self.repo, "show", "--resolved")
        self.assertIn("Verdict: APPROVED", result.stdout)

    def test_nested_checks_both_lists_shown(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        make_skill(self.repo, "lint-spec.py")
        make_skill(self.repo, "check-tests.py")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {"checks": [["lint-spec.py", "${plan_dir}/spec.md"], ["check-tests.py", "${plan_dir}"]]}},
                "phases": [{"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        result = run(self.repo, "show", "--resolved")
        self.assertEqual(result.returncode, 0)
        self.assertIn("lint-spec.py", result.stdout)
        self.assertIn("check-tests.py", result.stdout)

    def test_built_in_phase_without_unattended_safe_ok(self) -> None:
        make_skill(self.repo, "plugin/skills/a/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "a", "type": "t", "skill": "${plugin_root}/skills/a/SKILL.md"}],
            },
        )
        result = run(self.repo, "check", plugin_root=self.repo / "plugin")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_foreign_phase_without_unattended_safe_fails(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md"}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'a'", result.stdout)

    def test_injected_copy_hash_mismatch_without_unattended_safe(self) -> None:
        path = make_skill(self.repo, ".factory/skills/scope/SKILL.md", "original\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = self.repo / ".factory" / ".inject.json"
        manifest.write_text(json.dumps({"plugin": "factory", "version": "1.0.0", "files": {"skills/scope/SKILL.md": digest}}))
        path.write_text("edited\n")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "scope", "type": "t", "skill": "${repo_root}/.factory/skills/scope/SKILL.md"}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("skills/scope/SKILL.md", result.stdout)
        self.assertIn("'scope'", result.stdout)

    def test_injected_copy_hash_match_without_unattended_safe_ok(self) -> None:
        path = make_skill(self.repo, ".factory/skills/scope/SKILL.md", "original\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        manifest = self.repo / ".factory" / ".inject.json"
        manifest.write_text(json.dumps({"plugin": "factory", "version": "1.0.0", "files": {"skills/scope/SKILL.md": digest}}))
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "scope", "type": "t", "skill": "${repo_root}/.factory/skills/scope/SKILL.md"}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_unparseable_manifest_without_unattended_safe(self) -> None:
        make_skill(self.repo, ".factory/skills/scope/SKILL.md", "original\n")
        (self.repo / ".factory" / ".inject.json").write_text("not json")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "scope", "type": "t", "skill": "${repo_root}/.factory/skills/scope/SKILL.md"}],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'scope'", result.stdout)
        self.assertIn(".inject.json", result.stdout)

    def test_interactive_after_unattended(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"unatt": {"interactive": False}, "inter": {"interactive": True}},
                "phases": [
                    {"id": "a", "type": "unatt", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True},
                    {"id": "b", "type": "inter", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True},
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("'a'", result.stdout)
        self.assertIn("'b'", result.stdout)

    def test_no_interactive_phase_go_none(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {"interactive": False}},
                "phases": [{"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        self.assertEqual(run(self.repo, "check").returncode, 0)
        result = run(self.repo, "show", "--resolved")
        self.assertIn("go: none", result.stdout)

    def test_builtin_default_go_after_scope(self) -> None:
        result = run(self.repo, "show", "--resolved", plugin_root=REPO_ROOT / "factory")
        self.assertIn("go: after scope", result.stdout)


class InitTest(TempRepoTestCase):
    def test_init_twice_second_fails(self) -> None:
        self.assertEqual(run(self.repo, "init").returncode, 0)
        result = run(self.repo, "init")
        self.assertEqual(result.returncode, 1)
        self.assertIn("config.yaml", result.stderr)


class SetUnsetTest(TempRepoTestCase):
    def _write_phase(self, pid: str = "build") -> None:
        make_skill(self.repo, "x/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": pid, "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )

    def test_set_phase_skill_roundtrips(self) -> None:
        self._write_phase()
        result = run(self.repo, "set", "build.skill", "${repo_root}/y/SKILL.md")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run(self.repo, "show")
        text = (self.repo / ".factory" / "config.yaml").read_text()
        self.assertIn("${repo_root}/y/SKILL.md", text)
        self.assertIn("id: build", text)
        self.assertIn("type: t", text)

    def test_set_defaults_attempts_reflected_in_show_resolved(self) -> None:
        self._write_phase()
        run(self.repo, "set", "defaults.attempts", "5")
        result = run(self.repo, "show", "--resolved")
        self.assertIn("attempts: 5", result.stdout)

    def test_set_types_checks_with_items(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        make_skill(self.repo, "lint-spec.py")
        make_skill(self.repo, "check-tests.py")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {},
                "phases": [{"id": "a", "type": "review", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        run(self.repo, "set", "types.review.checks", "--item", "lint-spec.py", "--item", "${plan_dir}/spec.md")
        result = run(self.repo, "show", "--resolved")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("lint-spec.py", result.stdout)
        self.assertIn("${plan_dir}/spec.md", result.stdout)

    def test_unset_last_key_drops_phase(self) -> None:
        self._write_phase()
        run(self.repo, "unset", "build.unattended_safe")
        run(self.repo, "unset", "build.skill")
        run(self.repo, "unset", "build.type")
        run(self.repo, "unset", "build.id")
        text = (self.repo / ".factory" / "config.yaml").read_text()
        self.assertNotIn("build", text)
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("empty phase list", result.stdout)

    def test_set_on_missing_phase_id(self) -> None:
        self._write_phase(pid="build")
        result = run(self.repo, "set", "nosuch.skill", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("nosuch", result.stderr)

    def test_set_malformed_path(self) -> None:
        result = run(self.repo, "set", "build")
        self.assertEqual(result.returncode, 3)
        self.assertIn("build", result.stderr)

    def test_set_twice_byte_identical(self) -> None:
        self._write_phase()
        run(self.repo, "set", "defaults.attempts", "5")
        before = (self.repo / ".factory" / "config.yaml").read_bytes()
        run(self.repo, "set", "defaults.attempts", "5")
        after = (self.repo / ".factory" / "config.yaml").read_bytes()
        self.assertEqual(before, after)

    def test_set_on_hand_written_file_reparses_to_same_pipeline(self) -> None:
        write_raw(
            self.repo,
            "version: 1\n"
            "types:\n"
            "  t:\n"
            "    interactive: false\n"
            "phases:\n"
            "  - id: build\n"
            "    type: t\n"
            "    skill: ${repo_root}/x/SKILL.md\n"
            "    unattended_safe: true\n",
        )
        make_skill(self.repo, "x/SKILL.md")
        before = run(self.repo, "show", "--resolved", "--json")
        run(self.repo, "set", "defaults.ceiling", "9")
        after_text = (self.repo / ".factory" / "config.yaml").read_text()
        self.assertTrue(after_text.startswith("# .factory/config.yaml\n"))
        after = run(self.repo, "show", "--resolved", "--json")
        self.assertEqual(
            [p["id"] for p in json.loads(before.stdout)["phases"]],
            [p["id"] for p in json.loads(after.stdout)["phases"]],
        )


class ResolutionTest(TempRepoTestCase):
    def test_no_config_file_shows_four_default_phases(self) -> None:
        result = run(self.repo, "show", "--resolved", plugin_root=REPO_ROOT / "factory")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("built-in default", result.stdout)
        for pid in ("scope", "scope-review", "build", "ship"):
            self.assertIn(pid, result.stdout)

    def test_config_with_two_phases_shows_exactly_those_in_order(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        make_skill(self.repo, "y/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {"id": "first", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True},
                    {"id": "second", "type": "t", "skill": "${repo_root}/y/SKILL.md", "unattended_safe": True},
                ],
            },
        )
        result = run(self.repo, "show", "--resolved", "--json")
        ids = [p["id"] for p in json.loads(result.stdout)["phases"]]
        self.assertEqual(ids, ["first", "second"])

    def test_plugin_root_placeholder_resolves_under_installed_plugin(self) -> None:
        plugin = self.repo / "plugin"
        make_skill(plugin, "skills/a/SKILL.md")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [{"id": "a", "type": "t", "skill": "${plugin_root}/skills/a/SKILL.md"}],
            },
        )
        result = run(self.repo, "show", "--resolved", "--json", plugin_root=plugin)
        self.assertEqual(result.returncode, 0, result.stdout)
        skill = json.loads(result.stdout)["phases"][0]["skill"]
        self.assertTrue(skill.startswith(str(plugin)))

    def test_relative_path_escaping_repo_root(self) -> None:
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {}},
                "phases": [
                    {"id": "a", "type": "t", "skill": "${repo_root}/../outside/SKILL.md", "unattended_safe": True}
                ],
            },
        )
        result = run(self.repo, "check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("outside", result.stdout)

    def test_phase_omitting_checks_inherits_type_checks(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        make_skill(self.repo, "lint-spec.py")
        make_skill(self.repo, "check-tests.py")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {"checks": [["lint-spec.py", "${plan_dir}/spec.md"]]}},
                "phases": [{"id": "a", "type": "t", "skill": "${repo_root}/x/SKILL.md", "unattended_safe": True}],
            },
        )
        result = run(self.repo, "show", "--resolved")
        self.assertIn("lint-spec.py", result.stdout)

    def test_phase_checks_replace_type_checks(self) -> None:
        make_skill(self.repo, "x/SKILL.md")
        make_skill(self.repo, "lint-spec.py")
        make_skill(self.repo, "check-tests.py")
        write_json_doc(
            self.repo,
            {
                "version": 1,
                "types": {"t": {"checks": [["lint-spec.py", "${plan_dir}/spec.md"]]}},
                "phases": [
                    {
                        "id": "a",
                        "type": "t",
                        "skill": "${repo_root}/x/SKILL.md",
                        "checks": [["check-tests.py", "${plan_dir}"]],
                        "unattended_safe": True,
                    }
                ],
            },
        )
        result = run(self.repo, "show", "--resolved")
        self.assertNotIn("lint-spec.py", result.stdout)
        self.assertIn("check-tests.py", result.stdout)

    def test_builtin_pipeline_exactly_three_checks_and_requires_prose(self) -> None:
        result = run(self.repo, "show", "--resolved", plugin_root=REPO_ROOT / "factory")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("check:"), 3)
        plugin = str(REPO_ROOT / "factory")
        for line in result.stdout.splitlines():
            if line.strip().startswith("check:"):
                executable = eval(line.split("check:", 1)[1])[0]  # noqa: S307 - our own printed list
                self.assertTrue(executable.startswith(plugin + "/"), executable)
                self.assertTrue(Path(executable).is_file(), executable)
        self.assertIn("gh pr view", result.stdout)
        self.assertIn("git ls-remote", result.stdout)
        self.assertIn("Verdict: APPROVED", result.stdout)

    def test_check_argv_with_plan_dir_left_literal(self) -> None:
        result = run(self.repo, "show", "--resolved", plugin_root=REPO_ROOT / "factory")
        self.assertIn("${plan_dir}", result.stdout)

    def test_builtin_scope_seals_plan_dir_spec_and_ceiling_twelve(self) -> None:
        result = run(self.repo, "show", "--resolved", plugin_root=REPO_ROOT / "factory")
        self.assertIn("seal: ${plan_dir}/spec.md", result.stdout)
        self.assertIn("ceiling: 12", result.stdout)

    def test_show_resolved_json_shape(self) -> None:
        result = run(self.repo, "show", "--resolved", "--json", plugin_root=REPO_ROOT / "factory")
        data = json.loads(result.stdout)
        self.assertIn("phases", data)
        for phase in data["phases"]:
            self.assertEqual(set(phase.keys()), {"id", "type", "skill", "interactive"})
        self.assertEqual([p["id"] for p in data["phases"]], ["scope", "scope-review", "build", "ship"])


if __name__ == "__main__":
    unittest.main()
