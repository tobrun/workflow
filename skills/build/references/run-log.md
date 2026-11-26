# Run Log

Every scope, build, and ship run leaves one record, so the workflow's effect can be measured across releases instead of judged by feel.
[../scripts/workflow-log.py](../scripts/workflow-log.py) writes it; the skill only calls it.

- As soon as the plan directory is known: `workflow-log.py start --phase {scope|build|ship} --plan .dev/{plan-name}`, by the path each skill's own text gives.
- As the run's last action, green or blocked: the same command with `end`. It reads the plan's own files for the facts (decisions and confidences, scenarios, deviations and fixups, the Quality table, open calls, review verdicts, the PR number) and the harness's session log for tokens, and queues one record.
- Ship's wrap-up then runs `workflow-log.py sync`, which pushes the queue to the private records repository, harvests each recorded PR's outcome (merged, hours to merge, changes requested, CI's verdict, and fix or revert commits on its files within 30 days of merge), and rewrites the scorecard `report.html` there.

The script never fails a run: an error, a missing repository, or no network is printed and the queue waits for the next sync.
Never write or edit a record by hand, and never let the record shape what the run does; `DEV_WORKFLOW_LOG=off` turns recording off for a run that must leave none.
A machine is set up once with `workflow-log.py setup --repo {git URL of the private records repository}`.
