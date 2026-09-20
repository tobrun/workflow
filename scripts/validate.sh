#!/usr/bin/env bash
# Self-validation script for the skills marketplace repository.
# Checks all structural rules and exits non-zero on first category of failure.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

ERROR_FILE=$(mktemp)
# Test output is kept (not discarded) so a failure can be diagnosed; CI uploads this directory.
if [ -n "${VALIDATE_LOGS:-}" ]; then
  LOG_DIR="$VALIDATE_LOGS"
  mkdir -p "$LOG_DIR"
  trap 'rm -f "$ERROR_FILE"' EXIT
else
  LOG_DIR=$(mktemp -d)
  # Keep the logs only when a check failed, since the failure messages point at them.
  trap 'if [ -s "$ERROR_FILE" ]; then echo "Logs kept in $LOG_DIR" >&2; else rm -rf "$LOG_DIR"; fi; rm -f "$ERROR_FILE"' EXIT
fi

# ---------------------------------------------------------------------------
# Helper: accumulate error message
# ---------------------------------------------------------------------------
fail() {
  local category="$1"
  local file="$2"
  local msg="$3"
  echo "[$category] $file: $msg" >> "$ERROR_FILE"
}

# ---------------------------------------------------------------------------
# Read one top-level field out of YAML frontmatter.
#
# Two incompatible programs ship as "yq": mikefarah's Go version takes
# `yq eval <expr> -`, kislyuk's Python wrapper takes `yq -r <expr>`. Detect
# which one is installed once, and fail loudly when neither works - swallowing
# the parser error would report every field as missing.
# ---------------------------------------------------------------------------
YQ_FLAVOR=""

detect_yq() {
  [ -n "$YQ_FLAVOR" ] && return
  if printf 'a: 1\n' | yq eval '.a' - >/dev/null 2>&1; then
    YQ_FLAVOR="go"
  elif printf 'a: 1\n' | yq -r '.a' >/dev/null 2>&1; then
    YQ_FLAVOR="python"
  else
    echo "validate.sh: no working yq found; install mikefarah/yq or python-yq" >&2
    exit 2
  fi
}

yaml_field() {
  local yaml="$1" key="$2"
  detect_yq
  if [ "$YQ_FLAVOR" = "go" ]; then
    printf '%s\n' "$yaml" | yq eval ".\"$key\" // \"\"" -
  else
    printf '%s\n' "$yaml" | yq -r ".\"$key\" // \"\""
  fi
}

# ---------------------------------------------------------------------------
# Get list of plugin directories from marketplace
# ---------------------------------------------------------------------------
marketplace_source_dirs() {
  jq -r '.plugins[] | .source | sub("^\\./"; "")' \
    ".claude-plugin/marketplace.json" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# Get list of actual plugin dirs (have .claude-plugin/plugin.json)
# ---------------------------------------------------------------------------
actual_plugin_dirs() {
  for dir in */; do
    dir="${dir%/}"
    [ "${dir:0:1}" = "." ] && continue
    [ "$dir" = "scripts" ] && continue
    [ "$dir" = "todo" ] && continue
    [ "$dir" = "plugins" ] && continue
    [ "$dir" = "research" ] && continue
    [ -f "$dir/.claude-plugin/plugin.json" ] && echo "$dir"
  done
}

# ===========================================================================
# R01: marketplace.json exists and is valid JSON
# ===========================================================================
check_01() {
  local f=".claude-plugin/marketplace.json"
  if [ ! -f "$f" ]; then
    fail "R01" "$f" "File not found"
    return
  fi
  if ! jq -e . "$f" >/dev/null 2>&1; then
    fail "R01" "$f" "Invalid JSON"
  fi
}

# ===========================================================================
# R02: Every entry in plugins[] has name, source, description
# ===========================================================================
check_02() {
  local f=".claude-plugin/marketplace.json"
  [ ! -f "$f" ] && return
  local entries
  entries=$(jq -c '.plugins[]' "$f" 2>/dev/null || true)
  [ -z "$entries" ] && return
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    local name source desc
    name=$(echo "$entry" | jq -r '.name // empty')
    source=$(echo "$entry" | jq -r '.source // empty')
    desc=$(echo "$entry" | jq -r '.description // empty')
    [ -z "$name" ]   && fail "R02" "$f" "Plugin entry missing 'name'"
    [ -z "$source" ] && fail "R02" "$f" "Plugin entry '$name' missing 'source'"
    [ -z "$desc" ]   && fail "R02" "$f" "Plugin entry '$name' missing 'description'"
  done <<< "$entries"
}

# ===========================================================================
# R03: Every source path exists as a directory
# ===========================================================================
check_03() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r source; do
    [ -z "$source" ] && continue
    [ ! -d "$source" ] && fail "R03" ".claude-plugin/marketplace.json" \
      "Source path './$source' is not a directory"
  done <<< "$sources"
}

# ===========================================================================
# R04: No orphans -- every plugin dir is listed AND every listing has a dir
# ===========================================================================
check_04() {
  local f=".claude-plugin/marketplace.json"
  local listed
  listed=$(marketplace_source_dirs)

  # Every actual plugin dir must be listed
  local actual
  actual=$(actual_plugin_dirs)
  if [ -n "$actual" ]; then
    while IFS= read -r dir; do
      [ -z "$dir" ] && continue
      if ! grep -qxF "$dir" <<< "$listed" 2>/dev/null; then
        fail "R04" "$dir" "Plugin directory not listed in marketplace.json"
      fi
    done <<< "$actual"
  fi

  # Every listing must have a matching directory
  if [ -n "$listed" ]; then
    while IFS= read -r entry; do
      [ -z "$entry" ] && continue
      if [ ! -d "$entry" ]; then
        fail "R04" "$f" "Plugin '$entry' listed but directory not found"
      elif [ ! -f "$entry/.claude-plugin/plugin.json" ]; then
        fail "R04" "$f" "Plugin '$entry' listed but no .claude-plugin/plugin.json"
      fi
    done <<< "$listed"
  fi
}

# ===========================================================================
# R05: Every plugin has plugin.json with name, version (semver),
#      description, author.name
# ===========================================================================
check_05() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    local pf="$dir/.claude-plugin/plugin.json"
    if [ ! -f "$pf" ]; then
      fail "R05" "$pf" "File not found"
      continue
    fi
    if ! jq -e . "$pf" >/dev/null 2>&1; then
      fail "R05" "$pf" "Invalid JSON"
      continue
    fi
    local name ver desc author
    name=$(jq -r '.name // empty' "$pf")
    ver=$(jq -r '.version // empty' "$pf")
    desc=$(jq -r '.description // empty' "$pf")
    author=$(jq -r '.author.name // empty' "$pf")

    [ -z "$name" ]   && fail "R05" "$pf" "Missing 'name'"
    [ -z "$desc" ]   && fail "R05" "$pf" "Missing 'description'"
    [ -z "$author" ] && fail "R05" "$pf" "Missing 'author.name'"
    if [ -z "$ver" ]; then
      fail "R05" "$pf" "Missing 'version'"
    else
      if ! echo "$ver" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$'; then
        fail "R05" "$pf" "Version '$ver' is not valid semver"
      fi
    fi
  done <<< "$sources"
}

# ===========================================================================
# R06: Dir name = plugin.json name = marketplace entry name
# ===========================================================================
check_06() {
  local f=".claude-plugin/marketplace.json"
  local entries
  entries=$(jq -c '.plugins[]' "$f" 2>/dev/null || true)
  [ -z "$entries" ] && return
  while IFS= read -r entry; do
    [ -z "$entry" ] && continue
    local mkt_name source
    mkt_name=$(echo "$entry" | jq -r '.name // empty')
    source=$(echo "$entry" | jq -r '.source // empty' | sed 's|^\./||')
    [ -z "$source" ] && continue
    [ ! -d "$source" ] && continue

    local dir_basename
    dir_basename=$(basename "$source")

    if [ "$dir_basename" != "$mkt_name" ]; then
      fail "R06" "$f" \
        "Dir name '$dir_basename' != marketplace entry name '$mkt_name'"
    fi

    local pj_name
    pj_name=$(jq -r '.name // empty' "$source/.claude-plugin/plugin.json" 2>/dev/null || echo "")
    if [ -n "$pj_name" ] && [ "$pj_name" != "$mkt_name" ]; then
      fail "R06" "$source/.claude-plugin/plugin.json" \
        "Name '$pj_name' != marketplace name '$mkt_name'"
    fi
  done <<< "$entries"
}

# ===========================================================================
# R07: Every plugin has a README.md
# ===========================================================================
check_07() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -f "$dir/README.md" ] && fail "R07" "$dir/README.md" "File not found"
  done <<< "$sources"
}

# ===========================================================================
# R08: Every plugin has at least one of skills/, commands/, agents/
# ===========================================================================
check_08() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    if [ ! -d "$dir/skills" ] && [ ! -d "$dir/commands" ] && [ ! -d "$dir/agents" ]; then
      fail "R08" "$dir" "No skills/, commands/, or agents/ directory found"
    fi
  done <<< "$sources"
}

# ===========================================================================
# R09: Every skills/*/ directory contains SKILL.md
# ===========================================================================
check_09() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -d "$dir/skills" ] && continue
    for skilldir in "$dir/skills"/*/; do
      [ ! -d "$skilldir" ] && continue
      skilldir="${skilldir%/}"
      [ ! -f "$skilldir/SKILL.md" ] && fail "R09" "$skilldir" "Missing SKILL.md"
    done
  done <<< "$sources"
}

# ===========================================================================
# R10: SKILL.md has YAML frontmatter with non-empty name and description
# ===========================================================================
check_10() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -d "$dir/skills" ] && continue
    for skilldir in "$dir/skills"/*/; do
      [ ! -d "$skilldir" ] && continue
      local sf="${skilldir%/}/SKILL.md"
      [ ! -f "$sf" ] && continue

      # Must start with ---
      if ! head -1 "$sf" | grep -q '^---$'; then
        fail "R10" "$sf" "Does not start with ---"
        continue
      fi

      # Extract YAML frontmatter (lines between first and second ---)
      local yaml
      yaml=$(awk '/^---$/ {c++; next} c==1 {print}' "$sf")
      if [ -z "$yaml" ]; then
        fail "R10" "$sf" "Empty or missing frontmatter"
        continue
      fi

      local n d
      n=$(yaml_field "$yaml" "name")
      d=$(yaml_field "$yaml" "description")
      [ -z "$n" ] && fail "R10" "$sf" "Frontmatter 'name' is empty or missing"
      [ -z "$d" ] && fail "R10" "$sf" "Frontmatter 'description' is empty or missing"
    done
  done <<< "$sources"
}

# ===========================================================================
# R11: Frontmatter name matches skill directory name
# ===========================================================================
check_11() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -d "$dir/skills" ] && continue
    for skilldir in "$dir/skills"/*/; do
      [ ! -d "$skilldir" ] && continue
      local sname
      sname=$(basename "${skilldir%/}")
      local sf="${skilldir%/}/SKILL.md"
      [ ! -f "$sf" ] && continue

      local yaml
      yaml=$(awk '/^---$/ {c++; next} c==1 {print}' "$sf")
      [ -z "$yaml" ] && continue

      local fm_name
      fm_name=$(yaml_field "$yaml" "name")
      if [ -n "$fm_name" ] && [ "$fm_name" != "$sname" ]; then
        fail "R11" "$sf" \
          "Frontmatter name '$fm_name' != directory name '$sname'"
      fi
    done
  done <<< "$sources"
}

# ===========================================================================
# R14: Every SKILL.md sets disable-model-invocation: true
# ===========================================================================
check_14() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -d "$dir/skills" ] && continue
    for skilldir in "$dir/skills"/*/; do
      [ ! -d "$skilldir" ] && continue
      local sf="${skilldir%/}/SKILL.md"
      [ ! -f "$sf" ] && continue

      local yaml
      yaml=$(awk '/^---$/ {c++; next} c==1 {print}' "$sf")
      [ -z "$yaml" ] && continue

      local dmi
      dmi=$(yaml_field "$yaml" "disable-model-invocation")
      if [ "$dmi" != "true" ]; then
        fail "R14" "$sf" \
          "Frontmatter must set 'disable-model-invocation: true' (all skills are human-triggered)"
      fi
    done
  done <<< "$sources"
}

# ===========================================================================
# R12: Every plugin appears in the root README table
# ===========================================================================
check_12() {
  local readme="README.md"
  [ ! -f "$readme" ] && { fail "R12" "$readme" "File not found"; return; }
  local names
  names=$(jq -r '.plugins[] | .name' ".claude-plugin/marketplace.json" 2>/dev/null || true)
  [ -z "$names" ] && return
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    if ! grep -q "\[$name\]" "$readme" 2>/dev/null; then
      fail "R12" "$readme" \
        "Plugin '$name' not found in plugin table (search for [$name])"
    fi
  done <<< "$names"
}

# ===========================================================================
# R13: No em dashes (U+2014) in any file
# ===========================================================================
check_13() {
  while IFS= read -r -d '' file; do
    local ft
    ft=$(file "$file" 2>/dev/null || echo "unknown")
    case "$ft" in
      *binary*|*image*|*archive*|*executable*|*compressed*|*font*)
        continue
        ;;
    esac
    if grep -E "$(printf '\xE2\x80\x94')" "$file" 2>/dev/null; then
      fail "R13" "$file" \
        "Contains em dash (Unicode U+2014). Use two plain dashes instead."
    fi
  done < <(find . -not -path './.git/*' -not -path './todo/*' -not -path './research/*' -not -path '*/__pycache__/*' -type f -print0)
}

# ===========================================================================
# R00 (bonus): Warn if SKILL.md is over ~200 lines
# ===========================================================================
check_skill_length() {
  local sources
  sources=$(marketplace_source_dirs)
  [ -z "$sources" ] && return
  while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    [ ! -d "$dir" ] && continue
    [ ! -d "$dir/skills" ] && continue
    for skilldir in "$dir/skills"/*/; do
      [ ! -d "$skilldir" ] && continue
      local sf="${skilldir%/}/SKILL.md"
      [ ! -f "$sf" ] && continue
      local total
      total=$(wc -l < "$sf")
      if [ "$total" -gt 200 ]; then
        fail "R00" "$sf" \
          "Body is $total lines (recommend under 150; move detail to references/)"
      fi
    done
  done <<< "$sources"
}

# ===========================================================================
# R15: every relative markdown link under every marketplace source resolves
# ===========================================================================
check_links() {
  local broken
  broken=$(marketplace_source_dirs | python3 -c '
import re, pathlib, sys

link_re = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")
sources = [line.strip() for line in sys.stdin if line.strip()]
paths = sorted(p for source in sources for p in pathlib.Path(source).rglob("*.md"))
for path in paths:
    text = path.read_text(encoding="utf-8")
    for target in link_re.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "/")):
            continue
        resolved = (path.parent / target).resolve()
        if not resolved.exists():
            print(f"{path}: broken link -> {target}")
')
  if [ -n "$broken" ]; then
    while IFS= read -r line; do
      fail "R15" "${line%%:*}" "${line#*: }"
    done <<< "$broken"
  fi
}

# ===========================================================================
# C01: Generated Codex distributions are current and structurally valid
# ===========================================================================
check_codex() {
  local marketplace=".agents/plugins/marketplace.json"

  if ! python3 scripts/build_codex_plugin.py --check >/dev/null 2>&1; then
    fail "C01" "plugins" \
      "Generated Codex plugins are stale; run python3 scripts/build_codex_plugin.py"
  fi

  if ! jq -e '.name == "nurbot" and (.plugins | length > 0)' \
    "$marketplace" >/dev/null 2>&1; then
    fail "C01" "$marketplace" "Invalid Codex marketplace"
    return
  fi

  local claude_names codex_names
  claude_names=$(jq -r '.plugins[].name' .claude-plugin/marketplace.json | sort)
  codex_names=$(jq -r '.plugins[].name' "$marketplace" | sort)
  if [ "$claude_names" != "$codex_names" ]; then
    fail "C01" "$marketplace" \
      "Codex plugin names ($(echo $codex_names)) != Claude plugin names ($(echo $claude_names))"
  fi

  local name
  while IFS= read -r name; do
    [ -z "$name" ] && continue
    local plugin="plugins/$name"
    local manifest="$plugin/.codex-plugin/plugin.json"

    if ! jq -e --arg name "$name" '
      .plugins[] | select(.name == $name) |
      .source.source == "local" and
      .source.path == ("./plugins/" + $name) and
      .policy.installation == "AVAILABLE" and
      .policy.authentication == "ON_INSTALL" and
      (.category | length > 0)
    ' "$marketplace" >/dev/null 2>&1; then
      fail "C01" "$marketplace" "Invalid Codex marketplace entry '$name'"
    fi

    if [ ! -d "$plugin" ]; then
      fail "C01" "$plugin" "Generated Codex plugin directory not found"
      continue
    fi

    if ! jq -e --arg name "$name" '
      .name == $name and
      (.version | test("^[0-9]+\\.[0-9]+\\.[0-9]+([+-][0-9A-Za-z.-]+)?$")) and
      (.description | length > 0) and
      (.author.name | length > 0) and
      .skills == "./skills/" and
      (.interface.displayName | length > 0) and
      (.interface.shortDescription | length > 0) and
      (.interface.longDescription | length > 0) and
      (.interface.developerName | length > 0) and
      (.interface.category | length > 0) and
      (.interface.capabilities | length > 0) and
      (.interface.defaultPrompt | length > 0)
    ' "$manifest" >/dev/null 2>&1; then
      fail "C01" "$manifest" "Invalid Codex plugin manifest"
    fi

    for skilldir in "$plugin"/skills/*/; do
      [ ! -d "$skilldir" ] && continue
      local skill_name
      skill_name=$(basename "${skilldir%/}")
      local skill_md="${skilldir%/}/SKILL.md"
      local agent_yaml="${skilldir%/}/agents/openai.yaml"

      if grep -q '^disable-model-invocation: true$' "$skill_md"; then
        fail "C01" "$skill_md" \
          "Codex skill must not contain Claude invocation frontmatter"
      fi
      if [ ! -f "$agent_yaml" ]; then
        fail "C01" "$agent_yaml" "Missing Codex skill interface metadata"
      elif ! grep -q '^  allow_implicit_invocation: false$' "$agent_yaml"; then
        fail "C01" "$agent_yaml" "Skill must remain explicit-invocation only"
      fi
      if ! grep -Fq "\$$name:$skill_name" "$agent_yaml" 2>/dev/null; then
        fail "C01" "$agent_yaml" \
          "Default prompt must name the namespaced Codex skill"
      fi
    done
  done <<< "$codex_names"
}

# ===========================================================================
# P01: Pi package manifest and subprocess review transport are valid
#
# Scoped to dev on purpose: Pi uses a flat skill namespace, and dev is the
# only plugin this repository ships to Pi.
# ===========================================================================
check_pi() {
  local package="package.json"
  local runner="dev/skills/ship/scripts/run-pi-agents.sh"
  local package_version
  local plugin_version

  if ! jq -e '
    .name == "@tobrun/dev-workflow" and
    (.version | test("^[0-9]+\\.[0-9]+\\.[0-9]+([+-][0-9A-Za-z.-]+)?$")) and
    (.keywords | index("pi-package")) and
    .files == ["dev/skills", "dev/references", "README.md"] and
    .pi.skills == ["./dev/skills"]
  ' "$package" >/dev/null 2>&1; then
    fail "P01" "$package" "Invalid Pi package manifest"
  fi

  package_version=$(jq -r '.version // empty' "$package" 2>/dev/null || true)
  plugin_version=$(jq -r '.version // empty' \
    dev/.claude-plugin/plugin.json 2>/dev/null || true)
  if [ "$package_version" != "$plugin_version" ]; then
    fail "P01" "$package" \
      "Pi package version must match dev/.claude-plugin/plugin.json"
  fi

  if [ ! -x "$runner" ]; then
    fail "P01" "$runner" "Pi agent runner must be executable"
  elif ! bash -n "$runner" scripts/test_pi_runner.sh; then
    fail "P01" "$runner" "Pi agent runner scripts have invalid shell syntax"
  elif ! bash scripts/test_pi_runner.sh >/dev/null 2>&1; then
    fail "P01" "$runner" "Pi agent runner self-test failed"
  fi

  if ! grep -q 'Native transport (preferred)' \
    dev/skills/ship/references/orchestration.md; then
    fail "P01" "dev/skills/ship/references/orchestration.md" \
      "Missing native multi-agent transport"
  fi
  if ! grep -q 'PI_CODING_AGENT=true' \
    dev/skills/ship/references/orchestration.md; then
    fail "P01" "dev/skills/ship/references/orchestration.md" \
      "Missing Pi subprocess transport"
  fi
}

# ===========================================================================
# F03: unattended factory material never routes a decision to a person
#
# Scoped to factory/skills/{scope-review,build,ship,run} (SKILL.md and their
# references) plus factory/references/factory-run.md only - scope keeps its
# interview, and ci-parity.md, contracts.md, and jira.md keep their dev
# wording because they are dev-shared references whose human-call branches are
# overridden for a factory run by factory-run.md's unattended policy.
#
# Each file's YAML frontmatter is skipped: a description states when a person
# invokes the skill, which is before the go, not a decision routed after it.
# ===========================================================================
check_factory_unattended() {
  local found
  found=$(python3 - <<'PYEOF'
import pathlib, re, sys

# A person noun, never the possessive ("the user's repo") or a compound
# ("a user-facing change"): those are prose about people, not routing to one.
PERSON = r"(?:user|operator|human|maintainer)s?\b(?![-'\u2019])"
ASK = r"ask(?:s|ed|ing)?|prompt(?:s|ed|ing)?|poll(?:s|ed|ing)?|quer(?:y|ies|ied|ying)|consult(?:s|ed|ing)?"
ROUTE = (r"confirm(?:s|ed|ing)?|check(?:s|ed|ing)?|wait(?:s|ed|ing)?\s+for|escalat(?:e|es|ed|ing)\s+to"
         r"|surfac(?:e|es|ed|ing)\s+to|defer(?:s|red|ring)?\s+to|hand(?:s|ed|ing)?(?:\s+off)?\s+to"
         r"|rout(?:e|es|ed|ing)\s+to")
DECIDES = (r"decides?|approves?|authorizes?|chooses?|confirms?|answers?|asks?|says?|accepts?|picks?"
           r"|selects?|responds?|has\s+(?:explicitly\s+)?asked|must\s+(?:decide|choose|answer|confirm)")
ARTICLE = r"(?:the\s+|a\s+|an\s+|each\s+|every\s+)?"
PATTERN = re.compile(
    rf"\b(?:{ASK})\s+{ARTICLE}{PERSON}"
    rf"|\b(?:{ROUTE})\s+{ARTICLE}{PERSON}"
    rf"|\b{ARTICLE}{PERSON}\s+(?:{DECIDES})\b"
    rf"|\b(?:question|decision|choice|call)s?\b[^.]{{0,40}}?\b(?:for|to|from)\s+{ARTICLE}{PERSON}"
    r"|\bhuman\s+calls?\b|\bstructured user-input tool\b|\bAskUserQuestion\b"
    r"|\bconfirm (?:with|before)\b",
    re.I,
)
SAMPLES = (
    "ask the user which one",
    "a human call",
    "both human calls",
    "the user decides",
    "confirm with the operator",
    "then any question left for the user",
    "full-repo runs only when the user asks",
    "prompt the user for a threshold",
    "surface to the maintainer",
    "offer the choice with AskUserQuestion",
    "wait for the human to answer",
    "escalate to a human",
    "the user accepts a different threshold",
)
NON_SAMPLES = (
    "the user's repository stays untouched",
    "a user-facing change needs an e2e scenario",
    "a whole user journey actually works",
    "no phase skill asks a person anything",
    "report failed naming what a person must supply",
)
for sample in SAMPLES:
    if not PATTERN.search(sample):
        print(f"scripts/validate.sh: F03 pattern no longer matches {sample!r}")
        sys.exit(0)
for sample in NON_SAMPLES:
    if PATTERN.search(sample):
        print(f"scripts/validate.sh: F03 pattern now falsely matches {sample!r}")
        sys.exit(0)

paths = []
for phase in ("scope-review", "build", "ship", "run"):
    root = pathlib.Path("factory/skills") / phase
    if root.is_dir():
        paths.extend(sorted(root.rglob("*.md")))
run_md = pathlib.Path("factory/references/factory-run.md")
if run_md.is_file():
    paths.append(run_md)

def body_lines(path):
    """Every line after the YAML frontmatter, numbered from 1 in the file."""
    lines = path.read_text(encoding="utf-8").splitlines()
    start = 0
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                start = index + 1
                break
    return enumerate(lines[start:], start=start + 1)


for path in paths:
    inside = False
    for number, line in body_lines(path):
        if "<!-- interactive-only -->" in line:
            inside = True
        elif "<!-- /interactive-only -->" in line:
            inside = False
        elif not inside and PATTERN.search(line):
            print(f"{path}:{number}: routes a decision to a person; decide under factory policy or mark the block <!-- interactive-only -->")
PYEOF
)
  if [ -n "$found" ]; then
    while IFS= read -r line; do
      fail "F03" "${line%%: *}" "${line#*: }"
    done <<< "$found"
  fi
}

# ===========================================================================
# F02: every factory phase writes the result protocol and never invokes
# another phase
# ===========================================================================
check_factory_protocol() {
  local found
  found=$(python3 - <<'PYEOF'
import pathlib, re, sys

PHASES = ("scope", "scope-review", "build", "ship")
INVOKE = re.compile(r"\$factory:([a-z-]+)|/SKILL\.md\b")

for phase in PHASES:
    sf = pathlib.Path("factory/skills") / phase / "SKILL.md"
    if not sf.is_file():
        print(f"{sf}: Factory phase skill not found")
        continue
    text = sf.read_text(encoding="utf-8")
    if "factory-run.json" not in text:
        print(f"{sf}: Must mention factory-run.json")
    if "results/{phase}-{attempt}.json" not in text:
        print(f"{sf}: Must mention the literal results/{{phase}}-{{attempt}}.json")
    for match in INVOKE.finditer(text):
        skill = match.group(1)
        if skill is None or skill != phase:
            print(f"{sf}: invokes another phase ({match.group(0)}); a phase skill never launches another")

run_md = pathlib.Path("factory/skills/run/SKILL.md")
if not run_md.is_file():
    print(f"{run_md}: run orchestrator skill not found")
    sys.exit(0)
run_text = run_md.read_text(encoding="utf-8")
for phase in PHASES:
    if phase not in run_text:
        print(f"{run_md}: must name phase '{phase}'")
if "run-state.py" not in run_text:
    print(f"{run_md}: must name run-state.py")
PYEOF
)
  if [ -n "$found" ]; then
    while IFS= read -r line; do
      fail "F02" "${line%%: *}" "${line#*: }"
    done <<< "$found"
  fi
}

# ===========================================================================
# F01: run-state.py's unit tests pass, excluding the harness that would
# otherwise recurse into this very check (D-nested-validate)
# ===========================================================================
check_factory_script() {
  local dir="factory/evals/tests"
  [ -d "$dir" ] || return
  if ! python3 - "$dir" >"$LOG_DIR/factory-script-tests.log" 2>&1 <<'PYEOF'
import sys, unittest

EXCLUDE = {"test_factory_checks"}


def leaves(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from leaves(item)
        else:
            yield item


directory = sys.argv[1]
loader = unittest.TestLoader()
discovered = loader.discover(start_dir=directory, top_level_dir=".")
cases = [t for t in leaves(discovered) if t.__class__.__module__.rsplit(".", 1)[-1] not in EXCLUDE]
modules = sorted({t.__class__.__module__.rsplit(".", 1)[-1] for t in cases})
print(f"discovered modules: {modules}")
suite = unittest.TestSuite(cases)
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
PYEOF
  then
    tail -60 "$LOG_DIR/factory-script-tests.log" >&2
    fail "F01" "$dir" \
      "Factory script tests failed (full output: $LOG_DIR/factory-script-tests.log); run: python3 -m unittest discover -s factory/evals/tests -t ."
  fi
}

# ===========================================================================
# Main
# ===========================================================================
check_01
check_02
check_03
check_04
check_05
check_06
check_07
check_08
check_09
check_10
check_11
check_12
check_13
check_14
check_skill_length
check_links
check_codex
check_pi
check_factory_unattended
check_factory_protocol
check_factory_script

if [ -s "$ERROR_FILE" ]; then
  echo ""
  echo "=== FAILED ==="
  cat "$ERROR_FILE"
  exit 1
fi

echo ""
echo "All checks passed."
