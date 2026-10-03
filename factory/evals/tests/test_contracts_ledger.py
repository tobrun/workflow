"""Change set 7: the contracts and ledger entries the config-driven factory lands.

Reads the real docs/ tree only.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = (REPO_ROOT / "docs" / "contracts.md").read_text(encoding="utf-8")
DECISIONS = (REPO_ROOT / "docs" / "decisions.md").read_text(encoding="utf-8")

NEW_CONTRACTS = ("C-factory-phase-contract", "C-factory-unattended", "C-factory-plan-files")
SUPERSEDED = (
    "D-phase-skills",
    "D-human-touchpoints",
    "D-handoff-seal",
    "D-failure-ladder",
    "D-unattended-wording-check",
)


def entry_names(text: str) -> list[str]:
    return re.findall(r"^(C-[a-z-]+):", text, flags=re.MULTILINE)


class ContractsLedgerTest(unittest.TestCase):
    def test_each_new_contract_appears_exactly_once(self) -> None:
        names = entry_names(CONTRACTS)
        for name in NEW_CONTRACTS:
            with self.subTest(name=name):
                self.assertEqual(names.count(name), 1)

    def test_the_old_factory_plugin_entries_are_gone(self) -> None:
        self.assertNotIn("C-factory-result", CONTRACTS)
        self.assertNotIn("## Factory plugin", CONTRACTS)
        self.assertEqual(sorted(entry_names(CONTRACTS)), sorted(NEW_CONTRACTS))

    def test_no_provenance_line_still_waits_on_change_set_7(self) -> None:
        self.assertNotIn("once change set 7 lands", CONTRACTS)
        self.assertNotIn("? verify", CONTRACTS)

    def test_the_superseded_decisions_carry_their_marks(self) -> None:
        for slug in SUPERSEDED:
            with self.subTest(slug=slug):
                line = next(l for l in DECISIONS.splitlines() if l.startswith(f"{slug}:"))
                self.assertIn("superseded by D-", line)

    def test_the_guarantees_the_contracts_name_exist(self) -> None:
        for path in ("scripts/validate.sh", "factory/skills/run/references/launch.md",
                     "factory/skills/run/scripts/run-state.py"):
            self.assertTrue((REPO_ROOT / path).is_file(), path)
        launch = (REPO_ROOT / "factory/skills/run/references/launch.md").read_text(encoding="utf-8")
        for literal in ("factory.result/1", "Never name or launch the next phase."):
            self.assertIn(literal, launch)


if __name__ == "__main__":
    unittest.main()
