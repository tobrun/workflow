# E2E Result Record

The e2e results are a strict JSON record with schema `factory.e2e/1`, written by the repository's e2e driver to `$FACTORY_E2E_OUT/e2e.json`.
The runner runs that driver itself at the gate, in a fresh output directory, and keeps only a record whose `revision` is the commit it tested and whose evidence files match their hashes.
It then publishes the record to `{report_dir}/{plan-name}-e2e.json` and renders `templates/e2e-report.html` from it with `scripts/render-e2e.py`; the record is the evidence and the HTML only its presentation.
Each scenario's `id` is the driver case name that `.dev/{plan-name}/scenario-map.json` maps an e2e scenario to.

```json
{
  "schema": "factory.e2e/1",
  "plan": "the .dev/{plan-name} slug",
  "title": "the spec's title",
  "generatedAt": "ISO timestamp of the run this record captures",
  "revision": "$FACTORY_REVISION, the commit the driver ran against",
  "kind": "frontend | non-frontend",
  "scenarios": [
    {
      "id": "expired-coupon",
      "title": "guest checks out with an expired coupon",
      "given": "...", "when": "...", "then": "...",
      "status": "pass | fail",
      "screenshots": [{"step": "coupon rejected", "caption": "...", "file": "expired-coupon/01-coupon.png", "sha256": "hex"}],
      "states": [{"step": "submit", "caption": "...", "entity": "orders", "before": {}, "after": {}}],
      "assertions": [{"name": "total unchanged", "passed": true, "detail": "optional"}],
      "output": "optional captured stdout or response body",
      "durationMs": 0
    }
  ]
}
```

## Rules the validator enforces

- The file is data only: strict JSON, no comments, expressions, duplicate keys, `NaN`, or `Infinity`, at most 8 MB.
- Unknown keys are rejected, so a misspelled field fails loudly instead of disappearing.
- Scenario `id`s are unique; totals are derived from the scenario records, and there is no `summary` field to hand-write.
- A scenario counts as failed when its `status` is `fail` or any of its `assertions` did not pass.
- Screenshot `file` paths are relative to the record's directory, never absolute or `..`, and each file must exist and match its `sha256`.
  Keep them under `$FACTORY_E2E_OUT`.
- A frontend case needs at least one screenshot and a non-frontend case at least one before/after state, or the runner rejects it.

## Filling it in honestly

- **Never fabricate a screenshot or a state entry.** Both come from an actual run of the actual application.
- **kind is chosen once per spec**: `"frontend"` when a user clicks through a UI a screenshot can show, `"non-frontend"` for a CLI, API, or batch job.
- **states are a valid supplement for a frontend scenario** when a step's real effect is invisible on screen.
- **status reflects what happened.** A scenario that failed and was then fixed is re-run and re-captured, never flipped.
- **assertions keep the verdict separate from presentation**: record each checked expectation with its real outcome.

## Legacy reports

Older runs embedded a JavaScript object in the HTML report.
`pr-evidence.py extract` still reads such a report when its block is strict JSON, for inspection only.
A block that is a JavaScript literal is never evaluated or rewritten: regenerate the evidence as a `factory.e2e/1` record.
