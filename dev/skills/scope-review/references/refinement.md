# One Spec Writer

Parallel reviewers may propose independent changes, but `spec.md` is one shared file.
Only the orchestrator writes it.
Do not let several agents read, replace, or save the whole document concurrently.

Give each refine agent the same settled spec and its SHA-256, its verified findings, and the authority rules from the skill.
The agent stays read-only in the repository and writes one proposal in the run's scratch directory:

```json
{
  "baseSha256": "sha256 of the spec bytes",
  "changes": [
    {"old": "exact unique excerpt from spec.md", "new": "replacement excerpt"}
  ]
}
```

Escalations are reported separately and receive no replacement.
When all proposals return, verify their scope and settled-decision authority.
Apply the independent proposals as one batch:

```bash
python3 {scope-review-skill-root}/scripts/apply-refinements.py .dev/{plan-name}/spec.md proposal-1.json proposal-2.json
```

The writer rejects stale bases, ambiguous excerpts, and overlapping edits without changing the spec.
Reconcile overlapping proposals yourself or obtain revised proposals against the new base.
Run `lint-spec.py` after applying, then verify the resulting meaning against the repo.
Serializing document writes does not require serializing the read-only review.
