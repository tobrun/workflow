# REVIEW_DATA Schema

The shape to populate in `templates/review.html` between the `REVIEW_DATA_START` / `REVIEW_DATA_END` markers, per the shared etiquette in [../../../references/reporting.md](../../../references/reporting.md).
This mirrors `review_N.md`'s structure exactly - it's the same content, published as the artifact a PR reviewer actually opens.

```js
const REVIEW_DATA = {
  title: "string - Review {N}: {title}",
  planName: "string - the .dev/{plan-name} slug, or empty if no spec was found",
  reviewIndex: 1,
  verdict: "PASS" | "CONCERNS" | "BLOCK",
  panel: ["lens", "..."],       // lenses run
  failedLenses: ["lens", "..."], // omit or [] if none failed
  base: "string - branch or PR reference",
  date: "string - ISO date",
  summary: "string - 1-2 sentence summary, markdown-lite",

  specConformance: "string - the spec-conformance summary, markdown-lite; omit the section entirely (set to '') if no spec was found",

  previousFindings: [
    { finding: "string", status: "fixed" | "still open" }
  ], // [] unless this is a re-review

  blockers: [
    { lenses: ["lens", "..."], file: "string", line: 0, title: "string", detail: "string - markdown-lite, the triggering scenario as confirmed by verification" }
  ],
  concerns: [
    { lenses: ["lens", "..."], file: "string", line: 0, title: "string", detail: "string" }
  ],
  nits: [
    { file: "string", lens: "string", issue: "string" }
  ],

  whatsGood: [
    { lens: "string", note: "string" }
  ],

  nextStep: "string - what to fix first and why, markdown-lite"
};
```

## Filling it in honestly

- This is a direct transcription of `review_N.md` into data, not a separate editorial pass - the two must agree.
- `blockers`/`concerns` only include findings that survived adversarial verification (CONFIRMED, or PLAUSIBLE demoted to CONCERN) - never a REFUTED finding.
- `previousFindings` is only non-empty on a re-review; omit the section in the template when empty rather than showing "none."
