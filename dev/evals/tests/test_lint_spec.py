"""Unit tests for lint-spec.py's echo check: a mismatched echo names what it should be.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_lint_spec.py' -t .
Each test writes a small spec to a scratch directory and runs the script as a
subprocess, so it proves the CLI contract the scope skill loops against.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dev.evals.tests.spec_fixtures import verification

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "source" / "scope" / "scripts" / "lint-spec.py"

CHOSEN = """D-storage: Where do files live?
  ✓ S3 bucket - durable across redeploys
  ✗ local disk - lost on redeploy
"""
OPEN = """D-storage: Where do files live? [open]
  ? S3 bucket - durable across redeploys
  ⚑ ask: expected retention?
"""
NOT_DOING = """D-storage: Where do files live?
  ✗ S3 bucket - no upload feature yet
  ⊘ not doing - nobody asked for uploads ? verify: support tickets; reopen on the first request
"""


def spec(decision: str, echo: str) -> str:
    return f"""# Title

## Research

{decision}
## Scope

Files go where D-storage ({echo}) says.
"""


class LintSpecEchoTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def lint(self, decision: str, echo: str) -> subprocess.CompletedProcess:
        path = Path(self.tmp.name) / "spec.md"
        path.write_text(spec(decision, echo), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False
        )

    def echo_errors(self, result: subprocess.CompletedProcess) -> str:
        return "\n".join(line for line in result.stdout.splitlines() if "echo" in line)

    def test_echo_copied_from_the_chosen_line_passes(self) -> None:
        self.assertEqual(self.echo_errors(self.lint(CHOSEN, "✓ S3")), "")

    def test_echo_not_in_the_chosen_line_names_the_chosen_text(self) -> None:
        result = self.lint(CHOSEN, "✓ cloud")
        self.assertIn("does not match its resolution", self.echo_errors(result))
        self.assertIn("s3 bucket", self.echo_errors(result))

    def test_chosen_echo_on_an_open_decision_expects_open(self) -> None:
        self.assertIn("expected (open)", self.echo_errors(self.lint(OPEN, "✓ S3")))

    def test_chosen_echo_on_a_not_doing_decision_expects_not_doing(self) -> None:
        self.assertIn("expected (⊘ not doing)", self.echo_errors(self.lint(NOT_DOING, "✓ S3")))

    def test_bare_check_mark_on_a_chosen_decision_passes(self) -> None:
        self.assertEqual(self.echo_errors(self.lint(CHOSEN, "✓")), "")

    def test_open_echo_on_a_chosen_decision_expects_the_chosen_text(self) -> None:
        result = self.lint(CHOSEN, "open")
        self.assertIn("does not match its resolution", self.echo_errors(result))
        self.assertIn("s3 bucket", self.echo_errors(result))

    def test_change_plan_linking_an_open_decision_is_flagged(self) -> None:
        path = Path(self.tmp.name) / "spec.md"
        path.write_text(
            spec(OPEN, "open") + "\n## Change plan\n\n1. Store files\n   a. `src/store.py` - write files - decisions: D-storage (open)\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
        self.assertIn("the change plan links D-storage, which is still open or flagged", result.stdout)

    def test_scope_echo_of_an_open_decision_is_not_a_change_plan_link(self) -> None:
        self.assertNotIn("change plan links", self.lint(OPEN, "open").stdout)

    def test_change_plan_linking_an_open_unflagged_decision_is_flagged(self) -> None:
        path = Path(self.tmp.name) / "spec.md"
        open_only = OPEN.replace("  ⚑ ask: expected retention?\n", "")
        path.write_text(
            spec(open_only, "open") + "\n## Change plan\n\n1. Store files\n   a. `src/store.py` - write files - decisions: D-storage (open)\n",
            encoding="utf-8",
        )
        result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False)
        self.assertIn("the change plan links D-storage", result.stdout)


class LintSpecReviewTagsTest(unittest.TestCase):
    """The impact tag and the Risk line feed render-spec.py, so lint requires both."""

    def lint(self, text: str) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.md"
            path.write_text(text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False
            ).stdout

    def test_decision_without_an_impact_tag_is_reported(self) -> None:
        self.assertIn("D-storage states no [impact: high|medium|low] tag", self.lint(spec(CHOSEN, "✓ S3")))

    def test_decision_with_an_impact_tag_passes(self) -> None:
        tagged = CHOSEN.replace("live?", "live? [impact: low]")
        self.assertNotIn("impact", self.lint(spec(tagged, "✓ S3")))

    def test_spec_without_a_risk_line_is_reported(self) -> None:
        self.assertIn("no 'Risk: low|medium|high - why' line", self.lint(spec(CHOSEN, "✓ S3")))

    def test_risk_line_above_the_first_section_passes(self) -> None:
        text = spec(CHOSEN, "✓ S3").replace("# Title\n", "# Title\n\nRisk: high - touches auth\n")
        self.assertNotIn("Risk:", self.lint(text))

    def test_risk_line_inside_a_section_does_not_count(self) -> None:
        text = spec(CHOSEN, "✓ S3").replace("## Scope\n", "## Scope\n\nRisk: high - touches auth\n")
        self.assertIn("no 'Risk: low|medium|high - why' line", self.lint(text))


class LintSpecVerificationTest(unittest.TestCase):
    """Every verification kind is planned yes or no, and the plan agrees with the scenario tags."""

    def lint(self, block: str, tests: str = "[unit] a -> b", extra: str = "") -> str:
        text = (f"# Title\n\nRisk: low - small\n\n## Research\n\n{CHOSEN.replace('live?', 'live? [impact: low]')}\n"
                f"## Scope\n\nFiles go where D-storage (✓ S3) says.\n\n### Summary\n\nThe change stores files in S3 so they survive a redeploy.\n\n### Validation\n\n- `make test`\n\n"
                f"{block}{extra}## Change plan\n\n1. Store\n   a. `src/store.py` - write - decisions: D-storage (✓ S3)\n"
                f"   tests: {tests}\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.md"
            path.write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False).stdout

    def test_a_complete_block_that_matches_the_tags_is_clean(self) -> None:
        self.assertIn("clean", self.lint(verification("unit")))

    def test_a_spec_without_the_block_is_reported(self) -> None:
        self.assertIn("no '### Verification' block in the scope section", self.lint(""))

    def test_every_kind_must_be_marked(self) -> None:
        block = verification("unit").replace("- penetration: no - not touched by this change\n", "")
        self.assertIn("the Verification block does not mark penetration", self.lint(block))

    def test_a_kind_without_its_reason_is_reported(self) -> None:
        block = verification("unit").replace("- fuzz: no - not touched by this change", "- fuzz: no")
        self.assertIn("fuzz: no states no ' - reason'", self.lint(block))

    def test_a_tag_needs_its_kind_marked_yes(self) -> None:
        self.assertIn("has [fuzz] scenarios but the Verification block marks fuzz: no",
                      self.lint(verification("unit"), "[unit] a -> b; [fuzz] random bytes -> no crash"))

    def test_a_kind_marked_yes_needs_a_scenario_with_its_tag(self) -> None:
        self.assertIn("marks property: yes but no change set has a [property] scenario", self.lint(verification("unit", "property")))

    def test_the_new_tags_are_layers(self) -> None:
        out = self.lint(verification("unit", "smoke", "benchmark"), "[unit] a -> b; [smoke] boot -> 200; [benchmark] 1k items -> under 5ms")
        self.assertIn("clean", out)

    def test_formal_names_its_mechanism(self) -> None:
        self.assertIn("formal: yes names no mechanism", self.lint(verification("unit", "formal")))
        self.assertIn("clean", self.lint(verification("unit", "formal", "property", how={"formal": "stateful [property] test of the order states"}),
                                         "[unit] a -> b; [property] any command sequence -> no illegal state"))

    LEAN = {"formal": "tile math as a Lean model with proofs (D-tile-model)"}
    THEOREMS = ("### Theorems\n\n- roundtrip: decoding an encoded tile gives the tile back\n  ```lean\n"
                "  def roundtrip : Prop := ∀ t : Tile, decode (encode t) = some t\n  ```\n"
                "  witness: `example : decode (encode ⟨3, 1, 2⟩) = some ⟨3, 1, 2⟩ := by decide`\n\n")
    DIFFERENTIAL = "[unit] a -> b; [property] any tile -> encode matches the Lean model's oracle"

    def test_formal_may_name_lean_verus_or_kani(self) -> None:
        for how in ("Verus proofs of the ring buffer", "Kani harnesses over the decoder"):
            self.assertNotIn("names no mechanism", self.lint(verification("unit", "formal", how={"formal": how})))

    def test_a_lean_mechanism_with_approvable_theorems_is_clean(self) -> None:
        out = self.lint(verification("unit", "formal", "property", how=self.LEAN), self.DIFFERENTIAL, self.THEOREMS)
        self.assertIn("clean", out)

    def test_a_lean_mechanism_needs_a_theorems_block(self) -> None:
        out = self.lint(verification("unit", "formal", "property", how=self.LEAN), self.DIFFERENTIAL)
        self.assertIn("formal: yes names Lean but the scope section has no '### Theorems' block", out)

    def test_a_lean_mechanism_needs_a_differential_test(self) -> None:
        out = self.lint(verification("unit", "formal", "property", how=self.LEAN), "[unit] a -> b; [property] any tile -> round-trips",
                        self.THEOREMS)
        self.assertIn("no [property] scenario checks the code against the model", out)

    def test_each_theorem_has_a_reading_a_statement_and_a_witness(self) -> None:
        block = self.THEOREMS.replace("roundtrip: decoding an encoded tile gives the tile back", "roundtrip:")
        block = block.replace("def roundtrip", "def round_trip").replace("  witness: `example", "  `example")
        out = self.lint(verification("unit", "formal", "property", how=self.LEAN), self.DIFFERENTIAL, block)
        self.assertIn("theorem roundtrip has no plain-English reading", out)
        self.assertIn("theorem roundtrip needs a ```lean fence stating 'def roundtrip : Prop := ...'", out)
        self.assertIn("theorem roundtrip has no 'witness:' line", out)

    def test_a_theorems_block_needs_a_lean_mechanism(self) -> None:
        out = self.lint(verification("unit"), extra=self.THEOREMS)
        self.assertIn("a '### Theorems' block needs formal: yes naming its Lean model", out)

    SUMMARY = "### Summary\n\nThe change stores files in S3 so they survive a redeploy.\n\n"

    def test_the_scope_needs_a_short_prose_summary(self) -> None:
        self.assertIn("clean", self.lint(verification("unit")))
        self.assertIn("no '### Summary' paragraph", self.lint_without_summary(verification("unit")))
        self.assertIn("the Summary is a paragraph, not a list", self.lint_without_summary(
            verification("unit"), "### Summary\n\n- stores files\n- in S3\n\n"))
        self.assertIn("the Summary runs 151 words", self.lint_without_summary(
            verification("unit"), "### Summary\n\n" + "word " * 151 + "\n\n"))

    def test_the_summary_ends_at_its_paragraph(self) -> None:
        after = self.SUMMARY + "Outputs:\n" + "".join(f"- output {n} with several words in it\n" for n in range(40)) + "\n"
        self.assertIn("clean", self.lint_without_summary(verification("unit"), after))

    def test_the_flow_block_holds_one_to_three_diagrams(self) -> None:
        diagram = "```mermaid\nsequenceDiagram\n  A->>B: call\n```\n\n"
        self.assertIn("clean", self.lint(verification("unit"), extra="### Flow\n\nThe call.\n\n" + diagram * 3))
        self.assertIn("the Flow block holds 4 diagrams", self.lint(verification("unit"), extra="### Flow\n\n" + diagram * 4))
        self.assertIn("the Flow block holds no ```mermaid diagram", self.lint(verification("unit"), extra="### Flow\n\nA sketch.\n\n"))

    def lint_without_summary(self, block: str, summary: str = "") -> str:
        text = (f"# Title\n\nRisk: low - small\n\n## Research\n\n{CHOSEN.replace('live?', 'live? [impact: low]')}\n"
                f"## Scope\n\nFiles go where D-storage (✓ S3) says.\n\n{summary}### Validation\n\n- `make test`\n\n"
                f"{block}## Change plan\n\n1. Store\n   a. `src/store.py` - write - decisions: D-storage (✓ S3)\n"
                f"   tests: [unit] a -> b\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.md"
            path.write_text(text, encoding="utf-8")
            return subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True, check=False).stdout

    def test_a_threat_model_is_required_and_needs_a_penetration_test(self) -> None:
        out = self.lint(verification("unit", "threat-model"))
        self.assertIn("threat-model: yes needs a '### Threat model' block", out)
        self.assertIn("threat-model: yes needs penetration: yes", out)
        threats = "### Threat model\n\n- upload endpoint: T tampered path -> normalize and reject '..' - tests: [unit] ../x -> 400\n\n"
        self.assertIn("clean", self.lint(verification("unit", "threat-model", "penetration"), extra=threats))
        self.assertIn("a Threat model line reads", self.lint(verification("unit", "threat-model", "penetration"),
                                                             extra="### Threat model\n\n- the endpoint is risky\n\n"))


class LintSpecInputTest(unittest.TestCase):
    def test_a_non_utf8_spec_is_an_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.md"
            path.write_bytes(b"# T\n\xff\xfe bad\n")
            result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("UTF-8", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_a_nested_numbered_list_is_not_a_change_set(self) -> None:
        import importlib.util
        module = importlib.util.spec_from_file_location("lint_spec", SCRIPT)
        lint = importlib.util.module_from_spec(module)
        module.loader.exec_module(lint)
        self.assertIsNotNone(lint.CHANGE_SET.match("1. Change set"))
        self.assertIsNone(lint.CHANGE_SET.match("   1. nested step"))
        body = [(1, "1. Change set"), (2, "   a. app.py - edit"), (3, "      1. nested step"),
                (4, "      2. second nested"), (5, "   tests: [unit] a -> b")]
        self.assertEqual(set(lint.change_set_files(body)), {1})


if __name__ == "__main__":
    unittest.main()
