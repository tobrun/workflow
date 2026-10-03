# E2E_DATA Schema

The shape to populate in `templates/e2e-report.html` between the `E2E_DATA_START` / `E2E_DATA_END` markers.
Replace the whole object per the shared etiquette in [../../../references/reporting.md](../../../references/reporting.md).

```js
const E2E_DATA = {
  title: "string - the spec's title",
  planName: "string - the .dev/{plan-name} slug",
  generatedAt: "string - ISO date of the run this report captures",

  // Decides how each scenario renders: a screenshot gallery, or a data-model-state table.
  kind: "frontend" | "non-frontend",

  scenarios: [
    {
      id: "string",
      title: "string - one line, e.g. 'guest checks out with an expired coupon'",
      given: "string",
      when: "string",
      then: "string",
      status: "pass" | "fail",

      // kind === "frontend": one entry per meaningful step, screenshot embedded as a data URI.
      screenshots: [
        { step: "string", caption: "string", dataUri: "data:image/png;base64,..." }
      ],

      // kind === "non-frontend": one entry per meaningful step. Also a valid supplement for a
      // frontend scenario when a step's real effect is a data change a screenshot can't show.
      dataModelState: [
        { step: "string", caption: "string", entity: "string", before: "object or string", after: "object or string" }
      ],

      logsOrOutput: "string - optional, captured stdout or response body",
      durationMs: 0
    }
  ],

  summary: { total: 0, passed: 0, failed: 0 }
};
```

## Filling it in honestly

- **Never fabricate a screenshot or a data-model-state entry.** Both must come from an actual run of the actual application - a screenshot invented to look plausible, or a before/after pair guessed instead of captured, defeats the entire point of this report being the enforceable proof behind an e2e criterion.
- **kind is chosen once per spec**, based on whether the system under test has a UI a screenshot could meaningfully show. A CLI, a backend API, a batch job: `"non-frontend"`. Anything a user clicks through: `"frontend"`.
- **dataModelState is a valid supplement even for a frontend scenario** when a step's real effect is invisible on screen (a queued job, a row written to a table the UI doesn't reflect yet).
- **status must reflect what actually happened.** A scenario that failed and was then fixed gets re-run and re-captured, not silently flipped to pass.
- **summary must match the scenarios array** - recompute it from the real counts, don't hand-write it separately.
