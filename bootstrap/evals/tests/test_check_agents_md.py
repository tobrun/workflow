"""Unit tests for bootstrap/skills/agents-md/scripts/check-agents-md.py.

Run from the repo root: python3 -m unittest discover -s bootstrap/evals/tests -t .
Each test builds a scratch repository and runs the checker as a subprocess, so it
proves the real CLI contract the agents-md skill loops against.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "bootstrap" / "skills" / "agents-md" / "scripts" / "check-agents-md.py"
EM_DASH = "\u2014"

CLEAN = """# Fixture service

A fixture service that exists to exercise the checker.
Default branch is `main`.

## Commands

- Setup: `npm install`
- Build: `npm run build`
- Check: `npm run check`
- Unit: `npm test`
- Integration: `npm run test:integration`
- E2E: none
- Merge gate: `make ci`

## Tests

Unit tests live next to the code in `src/`; integration tests live in `tests/integration/`.
Put each check on the cheapest layer that can fail for the right reason.

## Quality

Done means every merge-gate command passes locally.

## Architecture

The service reads `config/app.json` at startup.

## Contributions

Commit messages follow `type(scope): subject` with a What: and Why: body.

## Open items

- [ ] e2e: no layer drives the built service end to end.

## Maintaining this file

Keep only knowledge that almost every agent session needs.
"""


class CheckerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write("package.json", json.dumps({"scripts": {
            "build": "tsc", "check": "tsc --noEmit", "test": "vitest run", "test:integration": "vitest run tests",
        }}))
        self.write("Makefile", "ci: check\n\tnpm run check\n\ncheck:\n\tnpm test\n")
        self.write("src/index.ts", "export {};\n")
        self.write("tests/integration/store.test.ts", "export {};\n")
        self.write("config/app.json", "{}\n")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)

    def run_checker(self, text: str, *extra: str) -> subprocess.CompletedProcess:
        self.write("AGENTS.md", text)
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root / "AGENTS.md"), "--root", str(self.root), *extra],
            capture_output=True, text=True,
        )

    def assert_clean(self, text: str) -> subprocess.CompletedProcess:
        result = self.run_checker(text)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def assert_violation(self, text: str, fragment: str) -> None:
        result = self.run_checker(text)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn(fragment, result.stdout)


class ContractTest(CheckerTest):
    def test_a_clean_file_passes(self) -> None:
        result = self.assert_clean(CLEAN)
        self.assertIn("check-agents-md: ok", result.stdout)

    def test_a_missing_file_exits_two(self) -> None:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root / "AGENTS.md"), "--root", str(self.root)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("missing:", result.stdout)

    def test_an_unknown_argument_exits_two(self) -> None:
        self.write("AGENTS.md", CLEAN)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), str(self.root / "AGENTS.md"), "--bogus"], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 2)


class StructureTest(CheckerTest):
    def test_a_missing_section_is_a_violation(self) -> None:
        text = CLEAN.replace("## Quality\n\nDone means every merge-gate command passes locally.\n\n", "")
        self.assert_violation(text, "structure: missing '## Quality'")

    def test_sections_out_of_order_are_a_violation(self) -> None:
        text = CLEAN.replace("## Tests", "## Placeholder").replace("## Quality", "## Tests").replace("## Placeholder", "## Quality")
        self.assert_violation(text, "structure: sections out of order")

    def test_a_duplicated_section_is_a_violation(self) -> None:
        text = CLEAN.replace("## Architecture\n", "## Architecture\n\nFirst half.\n\n## Architecture\n")
        self.assert_violation(text, "'## Architecture' appears more than once")

    def test_maintaining_this_file_must_come_last(self) -> None:
        self.assert_violation(CLEAN + "\n## Company knowledge\n\nLives in the wiki.\n", "must be the last section")

    def test_an_extra_section_before_maintaining_passes(self) -> None:
        text = CLEAN.replace("## Open items", "## Company knowledge\n\nThe glossary lives in `config/app.json`.\n\n## Open items")
        self.assert_clean(text)

    def test_two_titles_are_a_violation(self) -> None:
        self.assert_violation("# Second title\n\n" + CLEAN, "expected exactly one '# ' title, found 2")

    def test_an_intro_without_the_default_branch_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("Default branch is `main`.\n", ""), "no 'Default branch is `x`' sentence")

    def test_headings_inside_a_code_fence_are_ignored(self) -> None:
        text = CLEAN.replace("## Architecture\n", "## Architecture\n\n```\n## Tests\n# not a title\n```\n")
        self.assert_clean(text)


class TextTest(CheckerTest):
    def test_a_file_over_the_line_cap_is_a_violation(self) -> None:
        result = self.run_checker(CLEAN, "--max-lines", "20")
        self.assertEqual(result.returncode, 1)
        self.assertIn("over the 20-line cap", result.stdout)

    def test_an_em_dash_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("fixture service that", f"fixture service {EM_DASH} that"), "em-dash: line 3")

    def test_a_leftover_fill_slot_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`make ci`", "<<fill: merge gate>>"), "placeholder:")

    def test_a_repeated_line_is_a_violation(self) -> None:
        line = "Put each check on the cheapest layer that can fail for the right reason.\n"
        self.assert_violation(CLEAN.replace("## Quality\n", "## Quality\n\n" + line), "duplicate:")


class SlotTest(CheckerTest):
    def test_a_missing_slot_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("- Build: `npm run build`\n", ""), "missing the '- Build:' slot")

    def test_none_without_its_open_item_is_a_violation(self) -> None:
        text = CLEAN.replace("- [ ] e2e: no layer drives the built service end to end.", "- [ ] ci: nothing gates pull requests.")
        self.assert_violation(text, "'E2E: none' (line 13) has no '- [ ] e2e:' open item")

    def test_na_with_a_reason_passes(self) -> None:
        self.assert_clean(CLEAN.replace("- Build: `npm run build`", "- Build: n/a (a library published as source)"))

    def test_a_prose_slot_value_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`npm run build`", "run the build script"), "must be a backticked command")

    def test_merge_gate_none_needs_a_ci_open_item(self) -> None:
        text = CLEAN.replace("- Merge gate: `make ci`", "- Merge gate: none")
        self.assert_violation(text, "'Merge gate: none'")
        self.assert_clean(text.replace("## Maintaining", "- [ ] ci: no workflow gates pull requests.\n\n## Maintaining").replace(
            "end to end.\n\n- [ ] ci", "end to end.\n- [ ] ci"))


class OpenItemTest(CheckerTest):
    def test_a_malformed_item_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("- [ ] e2e: no layer", "- e2e: no layer"), "is not '- [ ] slug: text'")

    def test_an_unknown_slug_is_a_violation(self) -> None:
        text = CLEAN.replace("drives the built service end to end.", "drives the built service end to end.\n- [ ] vibes: unclear.")
        self.assert_violation(text, "unknown slug 'vibes'")

    def test_an_empty_open_items_section_is_a_violation(self) -> None:
        text = CLEAN.replace("- E2E: none", "- E2E: `npm run test:integration -- e2e`").replace(
            "- [ ] e2e: no layer drives the built service end to end.\n", "")
        self.assert_violation(text, "'## Open items' is empty")

    def test_a_path_inside_an_open_item_is_not_checked(self) -> None:
        text = CLEAN.replace("end to end.", "end to end; add one under `e2e/journeys/`.")
        self.assert_clean(text)


class PathTest(CheckerTest):
    def test_a_stale_path_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("config/app.json", "config/settings.json"), "stale: `config/settings.json`")

    def test_a_missing_but_gitignored_path_passes(self) -> None:
        self.git("init", "-q")
        self.write(".gitignore", "dist/\n")
        self.assert_clean(CLEAN.replace("Unit tests live", "Builds land in `dist/`.\nUnit tests live"))

    def test_a_url_and_a_git_ref_are_not_paths(self) -> None:
        text = CLEAN.replace("Unit tests live", "Docs are at `https://example.com/a/b.md`; compare with `origin/main`.\nUnit tests live")
        self.assert_clean(text)

    def test_a_bracketed_route_file_is_a_literal_path(self) -> None:
        self.write("src/pages/[slug].astro", "---\n---\n")
        self.assert_clean(CLEAN.replace("Unit tests live", "Routes such as `src/pages/[slug].astro` render posts.\nUnit tests live"))

    def test_a_bare_filename_found_anywhere_in_the_repo_passes(self) -> None:
        self.write("tests/conftest.py", "\n")
        self.assert_clean(CLEAN.replace("Unit tests live", "Shared fixtures sit in `conftest.py`.\nUnit tests live"))

    def test_a_naming_convention_is_not_a_path(self) -> None:
        self.assert_clean(CLEAN.replace("Unit tests live", "Name posts `kebab-case.mdx` and components `PascalCase.tsx`.\nUnit tests live"))

    def test_a_framework_file_extension_is_checked(self) -> None:
        self.assert_violation(CLEAN.replace("Unit tests live", "The header is `Heder.astro`.\nUnit tests live"), "stale: `Heder.astro`")

    def test_a_bare_filename_found_nowhere_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("Unit tests live", "See `ContactCard.tsx`.\nUnit tests live"), "stale: `ContactCard.tsx`")

    def test_a_stale_relative_link_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("Unit tests live", "See [the guide](docs/guide.md).\nUnit tests live"), "link 'docs/guide.md'")

    def test_the_plan_directory_is_exempt(self) -> None:
        self.assert_clean(CLEAN.replace("with a What: and Why: body.", "with a What: and Why: body.\nPlans live under `.dev/`."))


class CommandTest(CheckerTest):
    def test_a_missing_npm_script_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("npm run build", "npm run bundle"), "`bundle` is not a script in package.json")

    def test_cd_resolves_against_the_nested_package(self) -> None:
        self.write("packages/api/package.json", json.dumps({"scripts": {"test": "bun test"}}))
        self.assert_clean(CLEAN.replace("`npm test`", "`cd packages/api && bun run test`"))
        self.assert_violation(CLEAN.replace("`npm test`", "`cd packages/api && bun run lint`"), "not a script in packages/api/package.json")

    def test_cd_into_a_missing_directory_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`npm test`", "`cd packages/gone && npm test`"), "cds into missing `packages/gone`")

    def test_bun_runs_a_file_or_a_script(self) -> None:
        self.write("e2e/run.ts", "\n")
        self.assert_clean(CLEAN.replace("- E2E: none", "- E2E: `bun e2e/run.ts`").replace(
            "- [ ] e2e: no layer drives the built service end to end.", "- [ ] e2e-env: the run needs a live token."))
        self.assert_violation(CLEAN.replace("`npm test`", "`bun dev`"), "`dev` is not a script in package.json")

    def test_bun_test_is_a_builtin(self) -> None:
        self.assert_clean(CLEAN.replace("`npm test`", "`bun test`"))

    def test_a_missing_make_target_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`make ci`", "`make verify`"), "make target(s) verify not defined")

    def test_a_make_target_from_an_included_mk_file_passes(self) -> None:
        self.write("build/rules.mk", "verify:\n\ttrue\n")
        self.assert_clean(CLEAN.replace("`make ci`", "`make verify`"))

    def test_a_missing_script_file_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`npm run check`", "`python3 scripts/check.py`"), "`scripts/check.py` does not exist")

    def test_a_missing_wrapper_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`npm run check`", "`./gradlew check`"), "`./gradlew` does not exist")

    def test_a_just_recipe_is_resolved(self) -> None:
        self.write("justfile", "check:\n    npm run check\n")
        self.assert_clean(CLEAN.replace("`npm run check`", "`just check`"))
        self.assert_violation(CLEAN.replace("`npm run check`", "`just lint`"), "just recipe `lint` not defined")

    def test_a_task_is_resolved(self) -> None:
        self.write("Taskfile.yml", "version: '3'\ntasks:\n  check:\n    cmds:\n      - npm run check\n")
        self.assert_clean(CLEAN.replace("`npm run check`", "`task check`"))
        self.assert_violation(CLEAN.replace("`npm run check`", "`task lint`"), "task `lint` not defined")

    def test_an_unresolvable_runner_is_only_a_note(self) -> None:
        result = self.assert_clean(CLEAN.replace("`npm run check`", "`npx tsc --noEmit`"))
        self.assertIn("note: `npx tsc --noEmit`", result.stdout)

    def test_an_environment_prefix_is_skipped(self) -> None:
        self.assert_clean(CLEAN.replace("`npm test`", "`CI=1 npm test`"))

    def test_bun_running_a_declared_dependency_binary_is_only_a_note(self) -> None:
        self.write("package.json", json.dumps({
            "scripts": {"build": "tsc", "check": "tsc", "test": "vitest", "test:integration": "vitest"},
            "devDependencies": {"turbo": "2.0.0"},
        }))
        result = self.assert_clean(CLEAN.replace("`make ci`", "`bun turbo test --filter=core`"))
        self.assertIn("note: `bun turbo test --filter=core`", result.stdout)

    def test_bun_running_an_undeclared_name_is_a_violation(self) -> None:
        self.assert_violation(CLEAN.replace("`make ci`", "`bun turbo test`"), "`turbo` is not a script in package.json")

    def test_a_test_path_filter_must_prefix_a_real_path(self) -> None:
        self.assert_clean(CLEAN.replace("`npm run test:integration`", "`bun test tests/integ`"))
        self.assert_violation(CLEAN.replace("`npm run test:integration`", "`bun test tests/gone`"), "`tests/gone` matches nothing in .")

    def test_an_unknown_runner_in_commands_is_only_a_note(self) -> None:
        result = self.assert_clean(CLEAN.replace("`npm run check`", "`astro check`"))
        self.assertIn("note: `astro check`", result.stdout)


class BranchTest(CheckerTest):
    def setUp(self) -> None:
        super().setUp()
        self.git("init", "-q", "-b", "main")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "--allow-empty", "-m", "init")

    def point_origin_head(self, branch: str) -> None:
        self.git("update-ref", f"refs/remotes/origin/{branch}", "HEAD")
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")

    def test_a_matching_default_branch_passes(self) -> None:
        self.point_origin_head("main")
        self.assert_clean(CLEAN)

    def test_a_mismatched_default_branch_is_a_violation(self) -> None:
        self.point_origin_head("develop")
        self.assert_violation(CLEAN, "branch: the file says `main` but origin/HEAD is `develop`")

    def test_an_unresolvable_origin_head_is_a_note(self) -> None:
        result = self.assert_clean(CLEAN)
        self.assertIn("note: origin/HEAD does not resolve locally", result.stdout)


if __name__ == "__main__":
    unittest.main()
