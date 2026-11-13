# Mutation testing value experiment

Mutation testing earned its keep as a focused test-review tool in these two
small projects. It exposed gaps that full line and branch coverage missed.
It did not prove production ROI, and a perfect mutation score did not prove
adequate testing.

## Measured results

Every main arm has 100% line and branch coverage. A killed mutant is a
generated code change rejected by the test suite. Raw score is killed /
generated mutants, without exclusions.

| Project | Suite | Tests | Mutants killed | Raw score | Seeded defects caught |
| --- | --- | ---: | ---: | ---: | ---: |
| Checkout pricing | Baseline | 5 | 15/28 | 53.6% | 3/8 |
| Checkout pricing | Requirements expansion | 11 | 20/28 | 71.4% | 5/8 |
| Checkout pricing | Mutation-guided expansion | 11 | 26/28 | 92.9% | 8/8 |
| Access control | Baseline | 5 | 9/11 | 81.8% | 1/8 |
| Access control | Requirements expansion | 11 | 10/11 | 90.9% | 5/8 |
| Access control | Mutation-guided expansion | 11 | 11/11 | 100% | 8/8 |

The two expansions each add six test functions per project. Across projects,
requirements-based additions caught 10/16 defects; mutation-guided additions
caught 16/16, a difference of six defects or 37.5 percentage points.
Baseline caught 4/16. These denominators describe this diagnostic fault set,
not a production defect probability.

The access-control ablation adds only two tests addressing the literal
survivors. It kills 11/11 mutants but catches only 3/8 seeded defects. The
misses are admin lock bypass, editor expiry bypass, owner access with the
wrong role, viewer access, and a one-second expiry error.
This ablation uses fewer tests and is not the matched-budget comparison.

## Cost

Wall time includes process startup. Ordinary timings are the median of six
runs, three per experiment repeat. Mutation timings are the median of two
fresh-cache runs, including mutmut's initial unmodified-suite run.

| Project and suite | Ordinary tests | Full mutation run | Multiple |
| --- | ---: | ---: | ---: |
| Checkout baseline | 0.094 s | 3.152 s | 33.6x |
| Checkout requirements | 0.101 s | 3.270 s | 32.5x |
| Checkout guided | 0.098 s | 3.369 s | 34.5x |
| Access baseline | 0.095 s | 1.465 s | 15.4x |
| Access requirements | 0.098 s | 1.525 s | 15.6x |
| Access guided | 0.098 s | 1.549 s | 15.8x |

Both guided suites together took about 0.195 s ordinarily and 4.918 s under
mutation, approximately 25x. The saved regression tests add roughly 6 ms
to combined ordinary-suite time relative to baseline, within startup noise.
Human triage and test-writing effort were not measured. Six added test
functions is an output budget, not equal elapsed effort. Installation,
coverage collection, and seeded-fault evaluation are outside mutation times.
These tiny pure functions cannot establish cost on an integration-heavy repo.

## What changed about quality review

- Coverage stayed at 100% while test sensitivity changed substantially.
- Checkout survivors prompted valid-zero and exact-100 boundary checks,
  asymmetric membership cases, exact discount amounts, and discount stacking.
- Access survivors prompted exact-expiry and role/ownership checks. Reviewing
  the surrounding policy also prompted privileged-role lock/expiry tests and
  the just-before-expiry case. These broader additions were inspired by the
  findings; they were not all directly demanded by surviving mutations.
- The last two checkout survivors change exception-message wording. That
  wording is observable, but outside the written contract. They are not
  equivalent mutants. No assertions were added just to raise the score.
- Two different mutations can encode the same defect for integer inputs,
  such as `>= 100` becoming `> 100` or `>= 101`. Variant counts are not counts
  of distinct defects.

## Method and limitations

Two synthetic Python projects contain 24 executable statements in total.
The baseline intentionally exercises every branch while retaining plausible
weaknesses: a loose discounted-price assertion, missing boundaries, and denial
checks performed on users who would already be denied.

Baseline tests, requirements expansions, and the 16 seeded faults were written
before reading mutation output. Guided tests were written after inspecting
baseline survivors. The two main experiments ran twice with fresh workspaces
and mutation caches. Mutant counts, defect outcomes, and coverage agreed
exactly across repeats. No reported timeout, suspicious, or untested outcomes
occurred. Tests ran in separate processes with bytecode writing disabled.
The runner checks that mutmut restores the original source.

The same author designed the programs, tests, and faults. The seeded faults
are evaluated separately but are not a blinded independent holdout. Some
overlap mutation operators exactly; others change policy precedence or
expressions. The requirements arm is one particular expansion, not the
strongest possible requirements-based testing. A careful boundary/policy
review could produce these same tests without mutation tooling.
There is no random allocation, production bug dataset, team behavior
measurement, or statistical evidence of general superiority.

Tools: mutmut 2.4.4, pytest 8.3.3, coverage 7.6.1; Python 3.14.7 on macOS ARM.
The historical mutmut version is pinned to make this CLI experiment
reproducible, not offered as a current tooling recommendation.

## Workflow implication

For this workflow, the evidence supports an optional focused mutation pass
for changed decision-heavy logic, after normal tests pass. Review surviving
changes against the contract, add regression tests for meaningful gaps,
and use the findings to review neighboring boundary and policy cases.
Keep ordinary tests in the default fast loop. Avoid a universal score gate
or forcing tests for behavior the product does not promise.

The user adopted this implication on 2026-10-31; dev ship guidance
now selects changed decision-heavy logic and triages survivors against the contract
(see `docs/decisions.md`, D-mutation-focused).
To establish ROI, pilot on existing pricing, authorization, parsing, or state
transition modules and record reviewer minutes, actionable survivors,
duplicate/out-of-contract findings, added regression tests, runtime, and
performance on independently selected historical bugs. Compare against
equally timed requirements review, ideally with different reviewers.
The break-even condition is expected avoided-defect cost exceeding compute,
triage, and test-maintenance cost; this experiment measures only local
runtime and diagnostic sensitivity.

Google's 2021 study of 15 million mutants provides observational evidence of
test-suite improvements over time and links to historical real faults:
[Long Term Effects of Mutation Testing](https://research.google/pubs/long-term-effects-of-mutation-testing/).
Its scaling study describes changed-code review, filtering, and limiting
findings rather than indiscriminately mutating whole repositories:
[Practical Mutation Testing at Scale](https://research.google/pubs/practical-mutation-testing-at-scale-a-view-from-google/).
These provide broader context, not validation of our synthetic effect size.

## Reproduce

Run from the repository root, with network access for the initial installation:

```sh
python3 -m venv /tmp/mutation-value-venv
/tmp/mutation-value-venv/bin/python -m pip install -r tools/harden/mutation-value/requirements-lock.txt
/tmp/mutation-value-venv/bin/python tools/harden/mutation-value/run.py --output /tmp/mutation-value-new
/tmp/mutation-value-venv/bin/python tools/harden/mutation-value/run.py --output /tmp/mutation-value-ablation-new --projects access --suites score_only
```

Use new output directories: the runner refuses to overwrite an existing arm.
Run the main command again with another new directory for a repeat.
The runner writes per-arm source copies, full logs, coverage JSON, XML mutation
results, survivor diffs, and a results JSON. Generated caches stay outside
the repository. The newer runner also records package versions and input
hashes and explicitly flags suspicious/untested results in its XML export.

`evidence/first.json`, `repeat.json`, and `ablation.json` preserve measurements.
Per-arm evidence preserves first-run coverage, mutation XML, survivor diffs,
baseline-pass logs, and seeded-fault logs. `inputs-sha256.json` records the
experiment inputs at packaging time. `requirements-lock.txt` records installed
versions. `report.html` is the standalone visual report.
