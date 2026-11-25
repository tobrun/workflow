# Proofs

How `formal` is met with machine-checked proofs: a Lean 4 reference model with proved statements for deterministic logic, and Verus or Kani for Rust.
A proof checker rejects any invalid proof, so agents may write proofs freely. What it cannot check is whether the statement says the right thing, or whether the shipping code behaves like the model. The person approves the statements, the gate keeps the checker honest, and a differential test ties the code to the model.

## When a proof is the mechanism

Choose it, as a `D-` decision, when all three hold:

1. The behavior is a deterministic function or state machine with a clean spec: a codec, parser, round-trip, authorization rule, config validator, scheduler, geometry or tile math.
2. A bug would be costly, security-relevant, or silent (a wrong answer, not a crash).
3. The statement is short and stable next to the code. A statement as long and volatile as the code it describes is not worth proving.

When correctness lives in interleavings across processes, model-check TLA+ or Alloy instead. When the production code is Rust, prove it directly: Verus for functional correctness, Kani for bounded proofs of no panic, overflow, or out-of-bounds. Otherwise use a stateful `[property]` test.
Never ship Lean in place of the product's language. The model is a reference, not the implementation.

## The Lean project

```
formal/
  lean-toolchain          # pinned, e.g. leanprover/lean4:v4.36.0; a bump re-runs the gate and repairs proofs
  lakefile.toml           # a lean_lib with globs covering every module, and a lean_exe for the oracle
  {Pkg}/Model.lean        # the executable reference model: obviously correct, not fast
  {Pkg}/Spec.lean         # the human-owned statements
  {Pkg}/Theorems.lean     # the agent-written proofs
  Main.lean               # the oracle: reads inputs, prints the model's outputs as JSON
```

`Spec.lean` holds each statement as a named proposition, at column 0, with its plain-English reading as a docstring and its witness right after it:

```lean
namespace Tile.Spec

/-- Decoding an encoded tile gives the tile back. -/
def roundtrip : Prop := ∀ t : Tile, t.valid → decode (encode t) = some t

example : decode (encode ⟨3, 1, 2⟩) = some ⟨3, 1, 2⟩ := by decide

end Tile.Spec
```

Each statement `NAME` is proved by a theorem `NAME_holds` in the same namespace, whose type is the constant itself: `theorem roundtrip_holds : roundtrip := by ...`. A proof can never restate, rename, or weaken the claim.

## Who owns the statements

`scope` writes them in the spec's `### Theorems` block, and the user approves them with the spec. Specs are never committed, so `build` copies each statement verbatim into `Spec.lean`, which is the committed source of truth.
From then on no agent edits `Spec.lean`. A statement that turns out wrong or unprovable is a human call that goes back to `scope`, never something to fix in place.
In a pull request, CI lists every statement added, changed, or removed since the base under "Statements to review". That list, not the proofs, is what a person reads. A CODEOWNERS entry for `formal/**/Spec.lean` makes that review required.

## Tying the code to the model

The model proves nothing about the product until a differential `[property]` test runs both on the same generated inputs and compares them: the product's code against `lake exe oracle` (or a committed corpus of vectors the oracle produced).
A shrunk divergence is a bug in one of them; decide which before fixing.
Floating point gets its own statement with an explicit error bound. Never model doubles as reals or rationals and call them equal.

## The gate

[../scripts/proof-gate.py](../scripts/proof-gate.py) is the only judge of a proof; an agent's "it builds" is not one.
`proof-gate.py check --project formal --spec .dev/{plan-name}/spec.md` does five things:

- Scans every source, with comments and strings stripped, for constructs that widen what must be trusted: `axiom`, `native_decide`, `implemented_by`, `extern`, `unsafe`, `set_option debug.*`, `notation` and its relatives, `macro`, `syntax`, `elab`, and `run_cmd`.
- Runs `lake build`.
- Restates each `Spec.lean` statement as a theorem proved by its `_holds` proof, and checks its axioms against the allow-list `propext`, `Classical.choice`, and `Quot.sound`.
- Compares `Spec.lean` with the approved spec.
- With `--recheck`, replays the modules through lean4checker when it is installed, and notes the skip when it is not.

| Exit | Meaning | Gate |
| --- | --- | --- |
| 0 | every statement proved | - |
| 1 | an integrity violation: a banned construct, a foreign axiom, a proof of another statement, a statement that differs from the approved one | must pass |
| 2 | only unfinished work: `sorry`, `admit`, a missing proof, a failing build | fix and report |
| 3 | the gate could not run: no pinned toolchain, no `lake`, no statement | a setup gap |

Fixing an integrity violation means removing the construct, never finding a quieter way to say it. A real need for one is a human call.
While proving, read the goal state from the Lean language server after each step, rather than re-running `lake build` blind.

Elaborating Lean runs arbitrary code. Check proofs only where no secret is reachable: a sandbox, or a CI job with read-only permissions and no tokens.

## Rust

- **Verus**: specs as `requires`/`ensures` and `proof fn` lemmas, checked by `cargo verus verify`. The statements live in the spec like Lean's.
- **Kani**: `#[kani::proof]` harnesses with an explicit `#[kani::unwind]` bound, checked by `cargo kani`. A bounded proof is reported with its bound.

Neither may use `assume`, `admit`, or `#[verifier::external_body]` on code under proof without a human call.
