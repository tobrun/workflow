"""Unit tests for change set 2: the copied references, scripts, and their
backticked cross-references. Read-only against the real repo tree - safe for
check_factory_script's own discovery since nothing here invokes validate.sh.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
FACTORY = REPO_ROOT / "factory"

SKILL_ROOTS = {
    "{scope-skill-root}": FACTORY / "phases" / "scope",
    "{scope-review-skill-root}": FACTORY / "phases" / "scope-review",
    "{build-skill-root}": FACTORY / "phases" / "build",
    "{ship-skill-root}": FACTORY / "phases" / "ship",
    "{run-skill-root}": FACTORY / "skills" / "run",
}

BACKTICK = re.compile(r"`([^`\n]+)`")
# Only a {skill-root}-style cross-reference or an explicit relative path
# (../ or ./) is a repo-relative path claim; a bare "spec.md" or
# "docs/decisions.md" names a plan or consuming-repo file, not a path here.
SKILL_ROOT_PATH = re.compile(r"^\{[a-z-]+-skill-root\}/[\w./-]+\.(py|md|sh)$")
RELATIVE_PATH = re.compile(r"^\.\.?/[\w./-]+\.(py|md|sh)$")


def resolve(path_dir: Path, raw: str) -> Path | None:
    for placeholder, root in SKILL_ROOTS.items():
        if raw.startswith(placeholder):
            return (root / raw[len(placeholder):].lstrip("/")).resolve()
    return (path_dir / raw).resolve()


class ProtocolAndCopiesTest(unittest.TestCase):
    def test_copied_tree_holds_dev_references_and_factory_run(self) -> None:
        dev_reference_names = {p.name for p in (REPO_ROOT / "dev" / "references").glob("*.md")}
        factory_reference_names = {p.name for p in (FACTORY / "references").glob("*.md")}
        self.assertTrue(dev_reference_names.issubset(factory_reference_names))
        self.assertIn("factory-run.md", factory_reference_names)

        factory_script_names = {p.name for p in (FACTORY / "scripts").glob("*.py")}
        self.assertEqual(
            factory_script_names, {"skill-metrics.py", "architecture-check.py", "factory-config.py"}
        )

    def test_backticked_reference_and_script_paths_resolve_under_factory(self) -> None:
        problems = []
        for md_path in sorted(FACTORY.rglob("*.md")):
            if "templates" in md_path.parts:
                continue
            text = md_path.read_text(encoding="utf-8")
            for raw in BACKTICK.findall(text):
                if not (SKILL_ROOT_PATH.match(raw) or RELATIVE_PATH.match(raw)):
                    continue
                resolved = resolve(md_path.parent, raw)
                if not resolved.is_file():
                    problems.append(f"{md_path}: {raw!r} -> {resolved} does not exist")
        self.assertEqual(problems, [], "\n".join(problems))


if __name__ == "__main__":
    unittest.main()
