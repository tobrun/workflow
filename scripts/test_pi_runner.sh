#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNNER="$ROOT/dev/source/ship/scripts/run-pi-agents.sh"
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

mkdir -p "$TEMP_DIR/prompts" "$TEMP_DIR/bin"
printf 'lens alpha\n' > "$TEMP_DIR/prompts/alpha.md"
printf 'lens beta\n' > "$TEMP_DIR/prompts/beta.md"

cat > "$TEMP_DIR/bin/pi-test" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$PI_TEST_LOG"
payload=$(cat)
if [ "$payload" = "agent failure" ]; then
  echo "simulated failure" >&2
  exit 7
fi
printf '%s\n' "$payload"
EOF
chmod +x "$TEMP_DIR/bin/pi-test"

PI_BIN="$TEMP_DIR/bin/pi-test" \
PI_PROVIDER="test-provider" \
PI_MODEL="test-model" \
PI_REASONING_LEVEL="high" \
PI_TEST_LOG="$TEMP_DIR/args.log" \
  bash "$RUNNER" "$TEMP_DIR/prompts" "$TEMP_DIR/results"

cmp "$TEMP_DIR/prompts/alpha.md" "$TEMP_DIR/results/alpha.out"
cmp "$TEMP_DIR/prompts/beta.md" "$TEMP_DIR/results/beta.out"
test "$(wc -l < "$TEMP_DIR/args.log" | tr -d ' ')" = "2"
grep -q -- '--no-session' "$TEMP_DIR/args.log"
grep -q -- '--no-skills' "$TEMP_DIR/args.log"
grep -q -- '--provider test-provider' "$TEMP_DIR/args.log"
grep -q -- '--model test-model' "$TEMP_DIR/args.log"
grep -q -- '--thinking high' "$TEMP_DIR/args.log"

printf 'agent failure\n' > "$TEMP_DIR/prompts/beta.md"
if PI_BIN="$TEMP_DIR/bin/pi-test" \
  PI_TEST_LOG="$TEMP_DIR/args.log" \
  bash "$RUNNER" "$TEMP_DIR/prompts" "$TEMP_DIR/partial-results" \
  >/dev/null 2>&1; then
  echo "Expected a failed Pi agent to return non-zero" >&2
  exit 1
fi
cmp "$TEMP_DIR/prompts/alpha.md" "$TEMP_DIR/partial-results/alpha.out"
test -s "$TEMP_DIR/partial-results/beta.err"

test -f "$TEMP_DIR/partial-results/beta.failed"
test ! -e "$TEMP_DIR/partial-results/alpha.failed"

# A rerun clears stale results: beta now succeeds, so its failure marker must go.
printf 'lens beta\n' > "$TEMP_DIR/prompts/beta.md"
echo stale > "$TEMP_DIR/partial-results/gamma.out"
PI_BIN="$TEMP_DIR/bin/pi-test" PI_TEST_LOG="$TEMP_DIR/args.log" \
  bash "$RUNNER" "$TEMP_DIR/prompts" "$TEMP_DIR/partial-results" >/dev/null 2>&1
test ! -e "$TEMP_DIR/partial-results/beta.failed"
test ! -e "$TEMP_DIR/partial-results/beta.err"
cmp "$TEMP_DIR/prompts/beta.md" "$TEMP_DIR/partial-results/beta.out"

# Timeout: a hung agent is killed and marked failed.
cat > "$TEMP_DIR/bin/pi-hang" <<'EOF2'
#!/usr/bin/env bash
cat >/dev/null
sleep 30
EOF2
chmod +x "$TEMP_DIR/bin/pi-hang"
mkdir -p "$TEMP_DIR/hang-prompts"
printf 'x\n' > "$TEMP_DIR/hang-prompts/slow.md"
if PI_BIN="$TEMP_DIR/bin/pi-hang" bash "$RUNNER" --timeout 1 "$TEMP_DIR/hang-prompts" "$TEMP_DIR/hang-results" >/dev/null 2>&1; then
  echo "Expected a timed-out Pi agent to return non-zero" >&2
  exit 1
fi
test -f "$TEMP_DIR/hang-results/slow.failed"

# Concurrency cap: with --jobs 1 no two agents overlap.
cat > "$TEMP_DIR/bin/pi-serial" <<'EOF2'
#!/usr/bin/env bash
cat >/dev/null
if ! mkdir "$PI_TEST_LOCK" 2>/dev/null; then echo overlap >> "$PI_TEST_LOCK.log"; fi
sleep 0.5
rmdir "$PI_TEST_LOCK" 2>/dev/null
echo ok
EOF2
chmod +x "$TEMP_DIR/bin/pi-serial"
PI_BIN="$TEMP_DIR/bin/pi-serial" PI_TEST_LOCK="$TEMP_DIR/lock" \
  bash "$RUNNER" --jobs 1 "$TEMP_DIR/prompts" "$TEMP_DIR/serial-results" >/dev/null 2>&1
test ! -e "$TEMP_DIR/lock.log"

# Signal trap: killing the runner kills its agents.
PI_BIN="$TEMP_DIR/bin/pi-hang" bash "$RUNNER" "$TEMP_DIR/hang-prompts" "$TEMP_DIR/trap-results" >/dev/null 2>&1 &
runner_pid=$!
sleep 1
kill -TERM "$runner_pid"
wait "$runner_pid" 2>/dev/null || true
sleep 0.5
if pgrep -f "$TEMP_DIR/bin/pi-hang" >/dev/null 2>&1; then
  echo "Agents survived the runner's termination" >&2
  pkill -f "$TEMP_DIR/bin/pi-hang" || true
  exit 1
fi

echo "Pi agent runner passed."
