# tools/harden

Repo-fitted pieces the `ship` gauntlet reuses on every run; the analyzers themselves are the ecosystem's, run through `uvx` or `npx` so nothing is installed globally.

- `scope.sh [--python]` - the files the branch added or modified against the default branch, minus the standard exclusions and the generated `plugins/` tree.
- `added_lines.py` - filters `path:line: ...` findings on stdin to the lines the branch added, so pre-existing findings are counted, not fixed.
- `complexity_coverage.py RADON_JSON COVERAGE_JSON [--prefix factory/]` - coverage-weighted complexity (complexity squared scaled down by line coverage) for functions the branch added; the line is 10 per D-complexity-threshold in `docs/decisions.md`.
- `flaky.py --runs 5 MODULE ...` - runs unittest modules repeatedly in shuffled order and names any test whose outcome disagrees between runs.
- `cosmic-ray.toml` - mutation testing scope for the runner's new modules (cosmic-ray, through `uvx`).

The gauntlet, from the repository root:

```bash
files=$(tools/harden/scope.sh --python)
uvx ruff check --output-format concise $files | python3 tools/harden/added_lines.py          # static analysis
uvx semgrep scan --config p/python --metrics off --quiet $files                              # static security rules
uvx detect-secrets scan $(tools/harden/scope.sh)                                             # secrets
uvx vulture --min-confidence 60 $files | python3 tools/harden/added_lines.py                 # dead code
npx --yes jscpd@4 --min-tokens 50 --format python factory/runner factory/scripts factory/evals scripts  # clones
cd factory && uvx coverage run --source=runner,scripts -m unittest discover -s runner/tests -t . \
  && uvx coverage json -o /tmp/coverage.json && uvx radon cc -j runner scripts > /tmp/radon.json && cd .. \
  && python3 tools/harden/complexity_coverage.py /tmp/radon.json /tmp/coverage.json --prefix factory/
```

Dependency rules are skipped until the repository has a `docs/dependencies.md`.
The repository pins no Python dependencies (the runner is stdlib-only), so there is no manifest to audit.
