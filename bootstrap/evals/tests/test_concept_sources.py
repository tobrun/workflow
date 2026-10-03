"""Tripwires that keep the agents-md references, the checker, and the dev sources aligned.

Run from the repo root: python3 -m unittest discover -s bootstrap/evals/tests -t .
The concept map is distilled from dev skill files; these tests catch a renamed or
moved source and a slug or section that drifted between the map, the template,
and the checker. They cannot catch a source whose content changed in place.
"""

from __future__ import annotations

import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL = REPO_ROOT / "bootstrap" / "skills" / "agents-md"
ROW = re.compile(r"^\| `([a-z0-9-]+)` \|.*\| `([^`]+)` \|\s*$")


def load_checker():
    spec = importlib.util.spec_from_file_location("check_agents_md", SKILL / "scripts" / "check-agents-md.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def concept_rows() -> dict[str, str]:
    text = (SKILL / "references" / "concepts.md").read_text(encoding="utf-8")
    return {m.group(1): m.group(2) for line in text.splitlines() if (m := ROW.match(line))}


def template_skeleton() -> str:
    text = (SKILL / "references" / "template.md").read_text(encoding="utf-8")
    match = re.search(r"```markdown\n(.*?)\n```\n", text, re.DOTALL)
    assert match, "template.md has no ```markdown skeleton"
    return match.group(1)


class ConceptSourcesTest(unittest.TestCase):
    def test_every_checker_slug_has_a_concept_row_and_no_row_is_unknown(self) -> None:
        self.assertEqual(set(concept_rows()), load_checker().SLUGS)

    def test_every_concept_source_exists_in_the_dev_plugin(self) -> None:
        for slug, source in concept_rows().items():
            with self.subTest(slug=slug):
                self.assertTrue(source.startswith("dev/"), source)
                self.assertTrue((REPO_ROOT / source).is_file(), f"{slug}: {source} no longer exists")

    def test_every_commands_slot_maps_to_a_concept_slug(self) -> None:
        checker = load_checker()
        self.assertLessEqual(set(checker.SLOTS.values()), checker.SLUGS)


class TemplateAgreesWithCheckerTest(unittest.TestCase):
    def test_the_template_sections_are_the_required_sections_in_order(self) -> None:
        titles = re.findall(r"^## (.+)$", template_skeleton(), re.MULTILINE)
        self.assertEqual([t for t in titles if t != "Open items"], load_checker().SECTIONS)
        self.assertEqual(titles[-2:], ["Open items", "Maintaining this file"])

    def test_the_template_has_exactly_the_checker_slots(self) -> None:
        labels = re.findall(r"^- ([A-Za-z0-9 ]+?): <<fill", template_skeleton(), re.MULTILINE)
        self.assertEqual(labels, list(load_checker().SLOTS))

    def test_the_template_states_the_default_branch(self) -> None:
        self.assertRegex(template_skeleton(), load_checker().DEFAULT_BRANCH.pattern.replace("`([^`]+)`", "`<<fill"))


if __name__ == "__main__":
    unittest.main()
