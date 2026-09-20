#!/usr/bin/env bash
# Coverage for a suite whose subject scripts run as subprocesses.
#
# A plain `coverage run -m unittest` traces only the parent process, so a script the tests invoke
# through `subprocess.run([sys.executable, script, ...])` reads 0% and its coverage-weighted
# complexity score collapses to complexity squared - a false failure, not a finding.
#
# This wires coverage's documented subprocess support: a sitecustomize.py on PYTHONPATH calls
# coverage.process_startup() in every child, COVERAGE_PROCESS_START names the config, and both
# data_file and source are absolute so a child running in a temp cwd still writes to one place
# and still matches the sources.
#
# Usage: tools/harden/coverage_run.sh OUT_JSON -- <unittest args>
#   tools/harden/coverage_run.sh /tmp/coverage.json -- discover -s factory/evals/tests -t .
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
root=$(pwd)
out=$1; shift
[ "${1:-}" = "--" ] && shift

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
printf 'import coverage\ncoverage.process_startup()\n' >"$work/sitecustomize.py"
cat >"$work/coveragerc" <<EOF
[run]
parallel = True
data_file = $work/.coverage
source =
    $root/factory
    $root/dev
    $root/scripts
EOF

COVERAGE_PROCESS_START="$work/coveragerc" PYTHONPATH="$work" \
  uvx --with coverage --from coverage coverage run --rcfile="$work/coveragerc" -m unittest "$@"
uvx --from coverage coverage combine --rcfile="$work/coveragerc" >/dev/null
uvx --from coverage coverage json --rcfile="$work/coveragerc" -o "$out" >/dev/null
echo "coverage json written to $out"
