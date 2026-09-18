#!/usr/bin/env bash
# In-scope files for a ship gauntlet run: files the local branch added or modified against the default
# branch, minus the standard exclusions and the generated plugins/ tree.
# Usage: tools/harden/scope.sh [--python]   (prints one existing path per line)
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
default=$(git symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)
base=$(git merge-base HEAD "$default")
git diff --name-only --diff-filter=AM "$base" HEAD -- . ':!*lock*' ':!dist/' ':!build/' ':!*.min.*' ':!*.map' \
  ':!*.png' ':!*.jpg' ':!*.gif' ':!*.webp' ':!*.woff*' ':!*.ttf' ':!**/__snapshots__/' ':!plugins/' |
  while read -r path; do
    [ -f "$path" ] || continue
    if [ "${1:-}" != "--python" ]; then
      echo "$path"
    elif [[ "$path" == *.py ]] || head -1 "$path" | grep -q '^#!.*python'; then
      echo "$path"
    fi
  done
