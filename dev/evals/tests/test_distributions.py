"""Tests for the portable skills tree: build_distributions.py builds it, check_skills.py holds it to the spec.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_distributions.py' -t .
The generator runs against a miniature plugin in a scratch directory; the checker
runs as a subprocess against broken trees, the way validate.sh calls it.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import build_distributions as generator

REPO_ROOT = Path(__file__).resolve().parents[3]
CHECK = REPO_ROOT / "scripts" / "check_skills.py"


def write(root: Path, files: dict[str, str]) -> None:
    for path, text in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


MINI = {
    "mini/.claude-plugin/plugin.json": json.dumps({"name": "mini", "version": "1.2.3", "description": "d",
                                                   "author": {"name": "T"}}),
    "mini/source/alpha/SKILL.md": "---\nname: alpha\ndescription: Does alpha. Use when alpha.\n"
                                  "disable-model-invocation: true\n---\n\n# Alpha\n\n"
                                  "Follow [the rules](../../references/rules.md) and run "
                                  "`python3 {alpha-skill-root}/../../scripts/tool.py`; see [beta](../beta/SKILL.md).\n",
    "mini/source/beta/SKILL.md": "---\nname: beta\ndescription: Does beta. Use when beta.\n"
                                 "disable-model-invocation: true\n---\n\n# Beta\n\nRead [local](references/local.md).\n",
    "mini/source/beta/references/local.md": "Local detail.\n",
    "mini/references/rules.md": "Rules link [more](more.md) and [beta's detail](../source/beta/references/local.md).\n",
    "mini/references/more.md": "More.\n",
    "mini/references/unused.md": "Nobody links here.\n",
    "mini/scripts/tool.py": "print('tool')\n",
}


class GeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        write(self.root, MINI)
        skill = generator.SkillConfig("X", "x", "Use x", "Needs Python 3.10+.")
        plugin = generator.PluginConfig("mini", "Mini", "mini", ("Write",), ("p",), {"alpha": skill, "beta": skill})
        with patch.object(generator, "ROOT", self.root), patch.object(generator, "PLUGINS", {"mini": plugin}), \
                redirect_stdout(io.StringIO()):
            generator.write()
        self.out = self.root / "skills"

    def test_shared_files_are_vendored_into_the_skill_that_uses_them(self) -> None:
        alpha = self.out / "alpha"
        self.assertTrue((alpha / "references" / "rules.md").is_file())
        self.assertTrue((alpha / "references" / "more.md").is_file(), "a shared file a vendored file links to comes along")
        self.assertTrue((alpha / "scripts" / "tool.py").is_file())
        self.assertFalse((alpha / "references" / "unused.md").exists(), "only what the skill reaches is copied")
        self.assertFalse((self.out / "beta" / "scripts").exists())

    def test_links_and_skill_root_paths_are_rewritten_to_the_portable_layout(self) -> None:
        skill = (self.out / "alpha" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("[the rules](references/rules.md)", skill)
        self.assertIn("`python3 {alpha-skill-root}/scripts/tool.py`", skill)
        self.assertIn("[beta](../beta/SKILL.md)", skill)
        rules = (self.out / "alpha" / "references" / "rules.md").read_text(encoding="utf-8")
        self.assertIn("[beta's detail](../../beta/references/local.md)", rules)

    def test_frontmatter_keeps_only_spec_fields(self) -> None:
        skill = (self.out / "beta" / "SKILL.md").read_text(encoding="utf-8")
        header = skill.split("---")[1]
        self.assertNotIn("disable-model-invocation", header)
        self.assertIn("compatibility: Needs Python 3.10+.", header)
        self.assertIn('  version: "1.2.3"', header)

    def test_codex_installs_from_the_portable_tree_with_its_sidecars(self) -> None:
        self.assertIn("allow_implicit_invocation: false", (self.out / "alpha" / "agents" / "openai.yaml").read_text())
        manifest = json.loads((self.root / ".codex-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["skills"], "./skills/")
        self.assertEqual(manifest["version"], "1.2.3")
        self.assertFalse((self.root / "plugins").exists(), "no second copy for installers to find")

    def test_the_generated_tree_passes_the_checker(self) -> None:
        result = subprocess.run([sys.executable, str(CHECK), str(self.out)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_source_skill_without_its_claude_policy_is_refused(self) -> None:
        path = self.root / "mini/source/beta/SKILL.md"
        path.write_text(path.read_text().replace("disable-model-invocation: true\n", ""))
        skill = generator.SkillConfig("X", "x", "Use x", "c")
        plugin = generator.PluginConfig("mini", "Mini", "mini", ("Write",), ("p",), {"alpha": skill, "beta": skill})
        with patch.object(generator, "ROOT", self.root), patch.object(generator, "PLUGINS", {"mini": plugin}):
            with self.assertRaises(ValueError):
                generator.build_all(self.root / "again")


GOOD = "---\nname: {name}\ndescription: Does it. Use when it.\nmetadata:\n  version: \"1\"\n---\n\n# Body\n\n{body}\n"


class CheckerTest(unittest.TestCase):
    def check(self, files: dict[str, str]) -> subprocess.CompletedProcess:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        tree = Path(scratch.name) / "skills"
        write(tree, files)
        return subprocess.run([sys.executable, str(CHECK), str(tree)], capture_output=True, text=True)

    def test_a_clean_tree_passes(self) -> None:
        result = self.check({"one/SKILL.md": GOOD.format(name="one", body="See [x](references/x.md)."),
                             "one/references/x.md": "x"})
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_a_harness_specific_field_is_refused(self) -> None:
        text = GOOD.format(name="one", body="").replace("metadata:", "disable-model-invocation: true\nmetadata:")
        self.assertIn("'disable-model-invocation' is not an Agent Skills field", self.check({"one/SKILL.md": text}).stdout)

    def test_the_name_must_match_its_folder_and_be_kebab_case(self) -> None:
        out = self.check({"one/SKILL.md": GOOD.format(name="One_Skill", body="")}).stdout
        self.assertIn("is not lowercase kebab-case", out)
        self.assertIn("does not match its folder", out)

    def test_a_link_leaving_the_tree_or_missing_is_refused(self) -> None:
        out = self.check({"one/SKILL.md": GOOD.format(name="one", body="[a](../../references/a.md) [b](references/b.md)")}).stdout
        self.assertIn("leaves the skills tree; vendor the file into the skill", out)
        self.assertIn("references/b.md does not exist", out)

    def test_a_sibling_skill_link_is_allowed_but_an_unknown_skill_root_is_not(self) -> None:
        files = {"one/SKILL.md": GOOD.format(name="one", body="[two](../two/SKILL.md) `{zed-skill-root}/x.py`"),
                 "two/SKILL.md": GOOD.format(name="two", body="")}
        out = self.check(files).stdout
        self.assertNotIn("two/SKILL.md", out)
        self.assertIn("{zed-skill-root} names no skill in the set", out)

    def test_a_bundled_file_nothing_links_to_is_reported(self) -> None:
        out = self.check({"one/SKILL.md": GOOD.format(name="one", body=""), "one/scripts/orphan.py": ""}).stdout
        self.assertIn("one/scripts/orphan.py is bundled but nothing links to it from SKILL.md", out)

    def test_an_overlong_description_and_body_are_refused(self) -> None:
        text = GOOD.format(name="one", body="line\n" * 500).replace("Does it. Use when it.", "d" * 1100)
        out = self.check({"one/SKILL.md": text}).stdout
        self.assertIn("description must be 1 to 1024 characters (has 1100)", out)
        self.assertIn("keep it under 500", out)


if __name__ == "__main__":
    unittest.main()
