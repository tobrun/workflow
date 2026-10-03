#!/usr/bin/env bash
# Copy the webhook fixture to a temp directory, turn it into a fresh git
# repository, and give it a bare origin so a factory run can push offline.
#
# Usage: bash setup.sh [destination]
# Prints the destination path on stdout as its last line.
set -euo pipefail

FIXTURE_DIR="$(cd "$(dirname "$0")" && pwd)"
DEST="${1:-$(mktemp -d -t factory-fixture-XXXXXX)}"
ORIGIN="$(mktemp -d -t factory-fixture-origin-XXXXXX)"

mkdir -p "$DEST"
cp -R "$FIXTURE_DIR/." "$DEST/"
rm -f "$DEST/setup.sh"

cd "$DEST"
git init -q -b main
cat > .gitignore <<'EOF'
.dev/
__pycache__/
*.pyc
EOF
git add -A
git commit -q -m "chore: initial commit of the webhook fixture"

git init -q --bare "$ORIGIN"
git remote add origin "$ORIGIN"
git push -q -u origin main

echo "$DEST"
