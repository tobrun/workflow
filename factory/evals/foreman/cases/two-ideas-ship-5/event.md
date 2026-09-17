Factory run <run_id>: event attempt.finished.
ship attempt 5 (stage) ended failed [branch.unsynchronized]: codex exited 1; branch factory/two-ideas at 0912c78271d2 has 1 unpushed commit(s) (unexpected status 401 Unauthorized: The security token included in the request is expired, url: https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses, request id: req_fzpx5ppb72zihupxxajlat2huj7bx6wv77hwlb4jvju7hg367d5a; Reconnecting... 1/5 (unexpected status 401 Unauthorized: The security to)
Same failure code and unchanged tree 2 attempts in a row.
Files: gate <run_dir>/attempts/ship-5/gate.json; stderr <run_dir>/attempts/ship-5/stderr.log; result <worktree>/.dev/two-ideas/ship-result.json
Operator note: Pushed 0912c78 and refreshed Bedrock credentials. Continue ship from existing artifacts; re-review current HEAD and write a valid schema-2 ship-result.json.
Caps: ship attempts 5/6, repairs 0/3, wait 0.0/120 min, run 18.11/24 h, overrides 0/2, retries 2/2.
Reply with exactly one factory.decision/1 JSON object.
