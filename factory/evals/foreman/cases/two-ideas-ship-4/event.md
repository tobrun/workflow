Factory run <run_id>: event attempt.finished.
ship attempt 4 (stage) ended blocked [branch.unsynchronized]: branch factory/two-ideas at 0912c78271d2 has 1 unpushed commit(s); the stage result is invalid: ship-result.json is malformed: result: unknown key(s) 'commit', 'pr'; conditions[0]: must be an object [record.invalid]
Files: gate <run_dir>/attempts/ship-4/gate.json; last_message <run_dir>/attempts/ship-4/last-message.md; stderr <run_dir>/attempts/ship-4/stderr.log; result <worktree>/.dev/two-ideas/ship-result.json
Operator note: Pushed 0912c78 and refreshed Bedrock credentials. Continue ship from existing artifacts; re-review current HEAD and write a valid schema-2 ship-result.json.
Caps: ship attempts 4/6, repairs 0/3, wait 0.0/120 min, run 18.11/24 h, overrides 0/2, retries 2/2.
Reply with exactly one factory.decision/1 JSON object.
