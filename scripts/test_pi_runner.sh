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

echo "Pi agent runner passed."
