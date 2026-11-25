"""Red-team tests for proof-gate.py: every known way to fake a proof is refused.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_proof_gate.py' -t .
Each test writes a small Lake project and puts a fake `lake` first on PATH. The fake
answers `lake build` and, for `lake env lean`, prints what Lean would for each
restated statement in the gate file, as the PROOFS variable says: proved, sorry,
an extra axiom, a type mismatch, or a missing proof. When a real `lake` is
installed, one test runs the gate against real Lean.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "scripts" / "proof-gate.py"

FAKE_LAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
args = sys.argv[1:]
if args[:1] == ["build"]:
    print(os.environ.get("BUILD_OUTPUT", "Build completed successfully."))
    sys.exit(int(os.environ.get("BUILD_RC", "0")))
if args[:2] == ["env", "lean"]:
    path = args[2]
    proofs = json.loads(os.environ.get("PROOFS", "{}"))
    for number, line in enumerate(open(path, encoding="utf-8").read().splitlines(), start=1):
        match = re.match(r"theorem (ProofGate\.t(\d+)) : (\S+) := (\S+)", line)
        if not match:
            continue
        name, index, statement, proof = match.groups()
        outcome = proofs.get(statement, "proved")
        if outcome == "missing":
            print(f"{path}:{number}:31: error(lean.unknownIdentifier): Unknown identifier `{proof}`")
            print(f"'{name}' depends on axioms: [sorryAx]")
        elif outcome == "mismatch":
            print(f"{path}:{number}:31: error: type mismatch\n  {proof}\nhas type\n  True : Prop")
            print(f"'{name}' depends on axioms: [sorryAx]")
        elif outcome == "sorry":
            print(f"'{name}' depends on axioms: [propext, sorryAx]")
        elif outcome.startswith("axiom:"):
            print(f"'{name}' depends on axioms: [propext,\n {outcome[6:]}]")
        elif outcome == "none":
            print(f"'{name}' does not depend on any axioms")
        else:
            print(f"'{name}' depends on axioms: [propext, Classical.choice, Quot.sound]")
    sys.exit(1 if any(v in ("missing", "mismatch") for v in proofs.values()) else 0)
sys.exit(0)
'''

SPEC_LEAN = """import Tile.Model

namespace Tile.Spec

/-- Decoding an encoded tile gives the tile back. -/
def roundtrip : Prop := ∀ t : Tile, decode (encode t) = some t

example : decode (encode ⟨3, 1, 2⟩) = some ⟨3, 1, 2⟩ := by decide

/-- A quadkey has one digit per zoom level. -/
def quadkey_length : Prop :=
  ∀ t : Tile, (quadkey t).length = t.z

end Tile.Spec
"""

THEOREMS_LEAN = """import Tile.Spec

namespace Tile.Spec

-- a comment that says sorry is not a proof hole
theorem roundtrip_holds : roundtrip := by
  intro t; simp [encode, decode]

/- block comment: axiom native_decide /- nested -/ still a comment -/
theorem quadkey_length_holds : quadkey_length := by
  intro t; simp [quadkey]

def message : String := "sorry, axiom, native_decide in a string"

end Tile.Spec
"""

APPROVED = """# Tiles

## Scope

### Theorems

- roundtrip: decoding an encoded tile gives the tile back
  ```lean
  def roundtrip : Prop := ∀ t : Tile, decode (encode t) = some t
  ```
  witness: `example : decode (encode ⟨3, 1, 2⟩) = some ⟨3, 1, 2⟩ := by decide`
- quadkey_length: a quadkey has one digit per zoom level
  ```lean
  def quadkey_length : Prop :=
    ∀ t : Tile, (quadkey t).length = t.z
  ```
  witness: `#guard (quadkey ⟨2, 1, 1⟩).length == 2`

### Verification
"""


class ProofGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = self.tmp / "formal"
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        lake = bin_dir / "lake"
        lake.write_text(FAKE_LAKE, encoding="utf-8")
        lake.chmod(0o755)
        self.env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
        self.write("lean-toolchain", "leanprover/lean4:v4.36.0\n")
        self.write("lakefile.toml", 'name = "tile"\n[[lean_lib]]\nname = "Tile"\n')
        self.write("Tile/Model.lean", "structure Tile where\n  z : Nat\n  x : Nat\n  y : Nat\n")
        self.write("Tile/Spec.lean", SPEC_LEAN)
        self.write("Tile/Theorems.lean", THEOREMS_LEAN)
        (self.tmp / "spec.md").write_text(APPROVED, encoding="utf-8")

    def write(self, name: str, text: str) -> None:
        path = self.project / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def gate(self, *extra: str, proofs: dict[str, str] | None = None, **env: str) -> tuple[int, dict, str]:
        out = self.tmp / "report.json"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "check", "--project", str(self.project), "--json", str(out), *extra],
            capture_output=True, text=True, check=False,
            env={**self.env, "PROOFS": json.dumps(proofs or {}), **env})
        self.assertNotIn("Traceback", result.stderr)
        return result.returncode, json.loads(out.read_text(encoding="utf-8")), result.stdout

    def integrity_rules(self, report: dict) -> set[str]:
        return {finding["rule"] for finding in report["integrity"]}

    def inject(self, line: str) -> None:
        self.write("Tile/Theorems.lean", THEOREMS_LEAN.replace("end Tile.Spec", f"{line}\n\nend Tile.Spec"))

    # -- honest work ---------------------------------------------------------

    def test_an_honest_project_is_clean(self) -> None:
        code, report, out = self.gate("--spec", str(self.tmp / "spec.md"))
        self.assertEqual(code, 0, out)
        self.assertEqual(report["result"], "clean")
        self.assertEqual([t["name"] for t in report["theorems"]], ["Tile.Spec.roundtrip", "Tile.Spec.quadkey_length"])
        self.assertEqual({t["status"] for t in report["theorems"]}, {"proved"})
        self.assertEqual(report["theorems"][0]["reading"], "Decoding an encoded tile gives the tile back.")
        self.assertIn("2 of 2 statements proved", out)

    def test_a_proof_with_no_axioms_is_proved(self) -> None:
        code, report, _ = self.gate(proofs={"Tile.Spec.roundtrip": "none"})
        self.assertEqual(code, 0)
        self.assertEqual(report["theorems"][0]["status"], "proved")

    def test_banned_words_in_comments_and_strings_are_not_flagged(self) -> None:
        _, report, _ = self.gate()
        self.assertEqual(report["integrity"], [])
        self.assertEqual(report["unfinished"], [])

    # -- unfinished, not cheating --------------------------------------------

    def test_sorry_is_unfinished_not_cheating(self) -> None:
        self.write("Tile/Theorems.lean", THEOREMS_LEAN.replace("intro t; simp [quadkey]", "sorry"))
        code, report, _ = self.gate(proofs={"Tile.Spec.quadkey_length": "sorry"})
        self.assertEqual(code, 2)
        self.assertEqual(report["result"], "unfinished")
        self.assertEqual(self.integrity_rules(report), set())
        self.assertEqual(report["theorems"][1]["status"], "unfinished")

    def test_admit_is_unfinished(self) -> None:
        self.write("Tile/Theorems.lean", THEOREMS_LEAN.replace("intro t; simp [quadkey]", "admit"))
        code, report, _ = self.gate(proofs={"Tile.Spec.quadkey_length": "sorry"})
        self.assertEqual(code, 2)
        self.assertIn("admit", {f["rule"] for f in report["unfinished"]})

    def test_a_statement_with_no_proof_is_unfinished(self) -> None:
        code, report, _ = self.gate(proofs={"Tile.Spec.quadkey_length": "missing"})
        self.assertEqual(code, 2)
        self.assertEqual(report["theorems"][1]["status"], "unfinished")

    def test_a_failing_build_is_unfinished(self) -> None:
        code, report, _ = self.gate(BUILD_RC="1", BUILD_OUTPUT="error: unsolved goals")
        self.assertEqual(code, 2)
        self.assertIn("unsolved goals", report["unfinished"][0]["detail"])

    # -- cheating -------------------------------------------------------------

    def test_each_banned_construct_is_an_integrity_violation(self) -> None:
        cases = {
            "axiom": "axiom cheat : ∀ t : Tile, decode (encode t) = some t",
            "native_decide": "example : 2 + 2 = 4 := by native_decide",
            "decide +native": "example : 2 + 2 = 4 := by decide +native",
            "implemented_by": "@[implemented_by fastDecode] def decode' := decode",
            "extern": '@[extern "lean_decode"] opaque decodeC : Nat → Nat',
            "unsafe": "unsafe def peek : Nat := 0",
            "debug option": "set_option debug.skipKernelTC true in\ntheorem x : False := sorry",
            "notation": "local notation:35 a \" ∧ \" b => True",
            "macro": "macro \"trivial_proof\" : tactic => `(tactic| sorry)",
            "elaborator": "elab \"cheat\" : tactic => pure ()",
            "meta code": "run_cmd pure ()",
            "compiler trust": "theorem y : True := Lean.ofReduceBool _ _ rfl",
        }
        for rule, line in cases.items():
            rule = "native_decide" if rule == "decide +native" else rule
            with self.subTest(rule=rule, line=line):
                self.inject(line)
                code, report, _ = self.gate()
                self.assertEqual(code, 1)
                self.assertEqual(report["result"], "integrity")
                self.assertIn(rule, self.integrity_rules(report))

    def test_an_axiom_outside_the_allow_list_is_an_integrity_violation(self) -> None:
        code, report, _ = self.gate(proofs={"Tile.Spec.roundtrip": "axiom:Tile.cheat"})
        self.assertEqual(code, 1)
        self.assertIn("depends on Tile.cheat", report["integrity"][0]["detail"])

    def test_a_proof_of_a_different_statement_is_an_integrity_violation(self) -> None:
        code, report, _ = self.gate(proofs={"Tile.Spec.roundtrip": "mismatch"})
        self.assertEqual(code, 1)
        self.assertEqual(report["theorems"][0]["status"], "integrity")
        self.assertIn("wrong statement", self.integrity_rules(report))

    def test_a_weakened_statement_differs_from_the_approved_spec(self) -> None:
        self.write("Tile/Spec.lean", SPEC_LEAN.replace("decode (encode t) = some t", "True"))
        code, report, _ = self.gate("--spec", str(self.tmp / "spec.md"))
        self.assertEqual(code, 1)
        self.assertIn("roundtrip differs from the approved statement", json.dumps(report["integrity"]))

    def test_a_renamed_statement_is_missing_and_unapproved(self) -> None:
        self.write("Tile/Spec.lean", SPEC_LEAN.replace("def roundtrip", "def roundtrip'"))
        code, report, _ = self.gate("--spec", str(self.tmp / "spec.md"))
        self.assertEqual(code, 1)
        details = json.dumps(report["integrity"])
        self.assertIn("roundtrip is approved in the spec but missing from Spec.lean", details)
        self.assertIn("roundtrip' is in Spec.lean but not approved", details)

    def test_reformatting_an_approved_statement_is_not_a_change(self) -> None:
        self.write("Tile/Spec.lean", SPEC_LEAN.replace("def roundtrip : Prop := ∀", "def roundtrip : Prop :=\n  -- why\n  ∀"))
        code, _, out = self.gate("--spec", str(self.tmp / "spec.md"))
        self.assertEqual(code, 0, out)

    def test_recheck_without_lean4checker_is_a_note_not_a_violation(self) -> None:
        code, report, out = self.gate("--recheck")
        self.assertEqual(code, 0, out)
        self.assertIn("lean4checker is not installed", report["notes"][0])

    # -- setup gaps ------------------------------------------------------------

    def test_an_unpinned_toolchain_does_not_run(self) -> None:
        (self.project / "lean-toolchain").unlink()
        code, report, _ = self.gate()
        self.assertEqual(code, 3)
        self.assertIn("pin the toolchain", report["errors"][0])

    def test_a_project_without_statements_does_not_run(self) -> None:
        self.write("Tile/Spec.lean", "namespace Tile.Spec\nend Tile.Spec\n")
        code, report, _ = self.gate()
        self.assertEqual(code, 3)

    def test_no_lake_does_not_run(self) -> None:
        code, report, _ = self.gate(PATH="/nonexistent")
        self.assertEqual(code, 3)
        self.assertIn("lake is not on PATH", report["errors"][-1])

    # -- the statements CI shows a human ---------------------------------------

    def test_base_lists_the_statements_added_changed_and_removed(self) -> None:
        run = lambda *a: subprocess.run(["git", *a], cwd=self.tmp, check=True, capture_output=True)
        run("init", "-q")
        run("-c", "user.name=t", "-c", "user.email=t@t", "add", ".")
        run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
        changed = SPEC_LEAN.replace("decode (encode t) = some t", "t.z < 30 → decode (encode t) = some t")
        changed = changed.replace("/-- A quadkey", "/-- Zoom is bounded. -/\ndef zoom_bound : Prop := ∀ t : Tile, t.z ≤ 30\n\n/-- A quadkey")
        changed = changed.replace("def quadkey_length", "def quadkey_len")
        self.write("Tile/Spec.lean", changed)
        _, report, out = self.gate("--base", "HEAD", proofs={})
        names = {kind: [e["name"] for e in entries] for kind, entries in report["statements"].items()}
        self.assertEqual(names["added"], ["Tile.Spec.zoom_bound", "Tile.Spec.quadkey_len"])
        self.assertEqual(names["changed"], ["Tile.Spec.roundtrip"])
        self.assertEqual(names["removed"], ["Tile.Spec.quadkey_length"])
        self.assertIn("decode (encode t) = some t", report["statements"]["changed"][0]["before"])
        self.assertIn("statement added: Tile.Spec.zoom_bound - Zoom is bounded.", out)


@unittest.skipUnless(shutil.which("lake"), "no Lean toolchain installed")
class RealLeanTest(unittest.TestCase):
    def test_real_lean_tells_each_outcome_apart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            toolchain = subprocess.run(["lean", "--version"], capture_output=True, text=True).stdout
            version = re.search(r"version (\d[^,\s]*)", toolchain)
            pin = f"leanprover/lean4:v{version.group(1)}" if version else "leanprover/lean4:stable"
            (project / "lean-toolchain").write_text(pin + "\n", encoding="utf-8")
            (project / "lakefile.toml").write_text('name = "demo"\n[[lean_lib]]\nname = "Demo"\n', encoding="utf-8")
            (project / "Demo").mkdir()
            (project / "Demo/Spec.lean").write_text(
                "namespace Demo.Spec\n\n/-- Adding zero changes nothing. -/\ndef add_zero : Prop := ∀ n : Nat, n + 0 = n\n\nend Demo.Spec\n",
                encoding="utf-8")
            proofs = project / "Demo/Theorems.lean"

            def gate(proof: str, declaration: str = "theorem add_zero_holds : add_zero") -> int:
                proofs.write_text(f"import Demo.Spec\n\nnamespace Demo.Spec\n\n{declaration} := {proof}\n\nend Demo.Spec\n",
                                  encoding="utf-8")
                return subprocess.run([sys.executable, str(SCRIPT), "check", "--project", str(project)],
                                      capture_output=True, text=True).returncode

            self.assertEqual(gate("fun n => rfl"), 0)
            self.assertEqual(gate("sorry"), 2)
            self.assertEqual(gate("by intro n; native_decide"), 1)
            self.assertEqual(gate("trivial", "theorem other : True"), 2)
            self.assertEqual(gate("trivial", "theorem add_zero_holds : True"), 1)


if __name__ == "__main__":
    unittest.main()
