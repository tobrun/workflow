#!/usr/bin/env bash
set -uo pipefail

usage() {
  echo "Usage: run-pi-agents.sh [--jobs N] [--timeout SECONDS] <prompt-directory> <output-directory>" >&2
  exit 2
}

max_jobs="${PI_AGENT_JOBS:-4}"
timeout_secs="${PI_AGENT_TIMEOUT:-900}"
positional=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --jobs) [ "$#" -ge 2 ] || usage; max_jobs="$2"; shift 2 ;;
    --timeout) [ "$#" -ge 2 ] || usage; timeout_secs="$2"; shift 2 ;;
    -*) usage ;;
    *) positional+=("$1"); shift ;;
  esac
done
[ "${#positional[@]}" -eq 2 ] || usage
case "$max_jobs" in ''|*[!0-9]*|0) echo "--jobs must be a positive integer" >&2; exit 2 ;; esac
case "$timeout_secs" in ''|*[!0-9]*|0) echo "--timeout must be a positive integer of seconds" >&2; exit 2 ;; esac
set -- "${positional[@]}"

prompt_dir="$1"
output_dir="$2"
pi_bin="${PI_BIN:-pi}"

if [ ! -d "$prompt_dir" ]; then
  echo "Prompt directory not found: $prompt_dir" >&2
  exit 2
fi
if ! command -v "$pi_bin" >/dev/null 2>&1; then
  echo "Pi executable not found: $pi_bin" >&2
  exit 2
fi

mkdir -p "$output_dir"
prompts=("$prompt_dir"/*.md)
if [ ! -e "${prompts[0]}" ]; then
  echo "No .md prompts found in: $prompt_dir" >&2
  exit 2
fi

args=(
  --no-session
  --no-skills
  --no-extensions
  --no-prompt-templates
  --tools read,bash,grep,find,ls
  --print
)
if [ -n "${PI_PROVIDER:-}" ]; then
  args+=(--provider "$PI_PROVIDER")
fi
if [ -n "${PI_MODEL:-}" ]; then
  args+=(--model "$PI_MODEL")
fi
if [ -n "${PI_REASONING_LEVEL:-}" ]; then
  args+=(--thinking "$PI_REASONING_LEVEL")
fi

# Prefer a real timeout binary; otherwise a background watchdog inside run_agent.
timeout_bin=""
for candidate in timeout gtimeout; do
  if command -v "$candidate" >/dev/null 2>&1; then timeout_bin="$candidate"; break; fi
done

run_agent() {
  local prompt="$1" name="$2" output="$3" error="$4" child watchdog rc
  if [ -n "$timeout_bin" ]; then
    "$timeout_bin" "$timeout_secs" "$pi_bin" "${args[@]}" < "$prompt" > "$output" 2> "$error" &
    child=$!
  else
    "$pi_bin" "${args[@]}" < "$prompt" > "$output" 2> "$error" &
    child=$!
    ( sleep "$timeout_secs"; : > "$output_dir/$name.timedout"; kill -TERM "$child" 2>/dev/null
      sleep 2; kill -KILL "$child" 2>/dev/null ) &
    watchdog=$!
  fi
  trap 'kill -TERM "$child" 2>/dev/null; [ -n "${watchdog:-}" ] && kill "$watchdog" 2>/dev/null; exit 143' INT TERM
  wait "$child"
  rc=$?
  if [ -n "${watchdog:-}" ]; then
    kill "$watchdog" 2>/dev/null
    wait "$watchdog" 2>/dev/null
  fi
  if [ "$rc" -eq 124 ] || [ -e "$output_dir/$name.timedout" ]; then
    rm -f "$output_dir/$name.timedout"
    echo "timed out after ${timeout_secs}s" >> "$error"
    rc=124
  fi
  if [ "$rc" -eq 0 ]; then
    rm -f "$error"
  else
    printf '%s\n' "$rc" > "$output_dir/$name.failed"
  fi
  exit "$rc"
}

pids=()
names=()

cleanup() {
  trap - INT TERM EXIT
  local pid
  for pid in ${pids[@]+"${pids[@]}"}; do
    kill -TERM "$pid" 2>/dev/null
  done
  wait 2>/dev/null
}
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM
trap cleanup EXIT

running() {
  local count=0 pid
  for pid in ${pids[@]+"${pids[@]}"}; do
    if kill -0 "$pid" 2>/dev/null; then count=$((count + 1)); fi
  done
  echo "$count"
}

for prompt in "${prompts[@]}"; do
  name=$(basename "$prompt" .md)
  # A result from an earlier run must never pass for this one.
  rm -f "$output_dir/$name.out" "$output_dir/$name.err" "$output_dir/$name.failed" "$output_dir/$name.timedout"
  while [ "$(running)" -ge "$max_jobs" ]; do sleep 0.2; done
  run_agent "$prompt" "$name" "$output_dir/$name.out" "$output_dir/$name.err" &
  pids+=("$!")
  names+=("$name")
done

status=0
for index in "${!pids[@]}"; do
  if ! wait "${pids[$index]}"; then
    echo "Pi agent failed: ${names[$index]} (see $output_dir/${names[$index]}.err)" >&2
    status=1
  fi
done
pids=()

exit "$status"
