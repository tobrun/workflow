#!/usr/bin/env bash
set -uo pipefail

if [ "$#" -ne 2 ]; then
  echo "Usage: run-pi-agents.sh <prompt-directory> <output-directory>" >&2
  exit 2
fi

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

pids=()
names=()

for prompt in "${prompts[@]}"; do
  name=$(basename "$prompt" .md)
  output="$output_dir/$name.out"
  error="$output_dir/$name.err"
  "$pi_bin" "${args[@]}" < "$prompt" > "$output" 2> "$error" &
  pids+=("$!")
  names+=("$name")
done

status=0
for index in "${!pids[@]}"; do
  if wait "${pids[$index]}"; then
    rm -f "$output_dir/${names[$index]}.err"
  else
    echo "Pi agent failed: ${names[$index]}" >&2
    status=1
  fi
done

exit "$status"
