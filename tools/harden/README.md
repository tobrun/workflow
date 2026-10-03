# tools/harden

Repo-fitted pieces the `ship` gauntlet reuses on every run; the analyzers themselves are the ecosystem's, run through `uvx` or `npx` so nothing is installed globally.

- `scope.sh [--python]` - the files the branch added or modified against the default branch, minus the standard exclusions and the generated `plugins/` tree.
- `added_lines.py` - filters `path:line: ...` findings on stdin to the lines the branch added, so pre-existing findings are counted, not fixed.
- `complexity_coverage.py RADON_JSON COVERAGE_JSON [--prefix pkg/]` - coverage-weighted complexity (complexity squared scaled down by line coverage) for functions the branch added; the line is 10 per D-complexity-threshold in `docs/decisions.md`.
- `coverage_run.sh OUT_JSON -- <unittest args>` - coverage for a suite whose subject scripts run as subprocesses; a plain `coverage run` traces only the parent, so a script the tests invoke through `subprocess.run` reads 0% and its complexity score collapses to complexity squared.
- `flaky.py --runs 5 MODULE ...` - runs unittest modules repeatedly in shuffled order and names any test whose outcome disagrees between runs.
- `mutate.py FILE FUNC ... --tests MODULE ...` - function-scoped mutation testing: mutates named functions one change at a time and runs their fast tests.
- `vulture_whitelist.py` - the committed list of names a framework calls rather than repository code, such as `BaseHTTPRequestHandler` overrides; vulture reads it as an ordinary input file, so pass it alongside the scoped files and record deliberate API surface there instead of adding per-line ignores.

The gauntlet, from the repository root:

```bash
files=$(tools/harden/scope.sh --python)
uvx ruff check --output-format concise $files | python3 tools/harden/added_lines.py          # static analysis
uvx semgrep scan --config p/python --metrics off --quiet $files                              # static security rules
uvx detect-secrets scan $(tools/harden/scope.sh)                                             # secrets
uvx vulture --min-confidence 60 $files tools/harden/vulture_whitelist.py |
  python3 tools/harden/added_lines.py                                                        # dead code
npx --yes jscpd@4 --min-tokens 50 --format python scripts                                   # clones
```

Coverage-weighted complexity joins a radon report to a coverage report, so it
needs the suite run first:

```bash
tools/harden/coverage_run.sh /tmp/coverage.json -- discover -s factory/evals/tests -t .
uvx radon cc -j factory scripts > /tmp/radon.json
python3 tools/harden/complexity_coverage.py /tmp/radon.json /tmp/coverage.json --threshold 10
```

Use `coverage_run.sh` rather than a bare `coverage run`: this repository's tests
drive their subject scripts as subprocesses, which the parent process never sees.

Dependency rules are skipped until the repository has a `docs/dependencies.md`.
The repository pins no Python dependencies, so there is no manifest to audit.
