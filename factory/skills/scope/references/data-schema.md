# SPEC_DATA Schema

The shape to populate in `templates/spec.html` between the `SPEC_DATA_START` / `SPEC_DATA_END` markers.
Replace the whole object per the shared etiquette in [../../../references/reporting.md](../../../references/reporting.md).

```js
const SPEC_DATA = {
  title: "string - the spec's title",
  planName: "string - the .dev/{plan-name} slug",
  date: "string - YYYY-MM-DD, the spec header date",
  summary: "string - one or two sentences: the problem and the chosen direction",

  // One entry per decision, in the research section's impact order.
  decisions: [
    {
      id: "string - the D- slug, e.g. D-file-storage",
      question: "string - the decision phrased as a question",
      status: "decided | open | not-doing",
      alternatives: [
        {
          mark: "chosen | rejected | open",   // renders as ✓ / ✗ / ?
          text: "string - the alternative",
          because: "string - the because clause, evidence marks included",
          downside: "string - the ⚠ clause; omit when none"
        }
      ],
      notDoing: "string - the ⊘ line with its reopen condition; only when status is not-doing",
      flags: ["string - each ⚑ line still waiting on the user"]   // omit or [] when none
    }
  ],

  scope: {
    overview: "string - the one-sentence overall task",
    efforts: [
      {
        title: "string - effort name",
        description: "string - one sentence",
        decisions: ["D-slug (✓ choice)", "..."],   // echoes, omit when none
        children: [ /* same shape, nested sub-efforts */ ]
      }
    ],
    nonGoals: ["string - each ⊘ line with its because clause"],
    invariants: ["string - inputs/outputs/invariants/error handling worth surfacing"],
    validation: ["string - each real repo command from the Validation block"]
  },

  changeSets: [
    {
      n: 1,                       // matches the change plan numbering
      title: "string - one line",
      items: [ { file: "string - path", change: "string - what happens there", decisions: ["D-slug (✓ choice)"] } ],
      tests: [ { layer: "unit | integration | e2e | none", scenario: "string - input -> expected outcome, or the none-reason" } ]
    }
  ]
};
```

## Filling it in honestly

- **This object mirrors `spec.md`, section for section** - decisions, scope, and change sets come straight from the file you just wrote. A section left empty here reads as a gap in the spec.
- **Marks are the content** - the because clauses, ⚠ downsides, and ⊘ reopen conditions are why the artifact is worth opening; never flatten them into bare labels.
- **flags belong to open decisions only** - a decision the change plan links never carries one. Surface any that remain loudly rather than hiding them.
- **Don't invent a decision that wasn't argued** - a decision with one alternative and no because clause is a statement, not a decision; leave it out or argue it first.
- **tests entries are the acceptance criteria** - keep layer tags accurate; `build` and `ship` treat them as the enforceable spec.
