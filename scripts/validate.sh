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
# Scoped to factory/phases/* minus the built-in pipeline's interactive phases,
# plus factory/skills/run (SKILL.md and their references) and
# factory/references/factory-run.md only - scope keeps its interview, and ci-parity.md, contracts.md, and jira.md keep their dev
# wording because they are dev-shared references whose human-call branches are
# overridden for a factory run by factory-run.md's unattended policy.
#
# Each file's YAML frontmatter is skipped: a description states when a person
# invokes the skill, which is before the go, not a decision routed after it.
#
# The pattern fires on a routing construction (ask/route/decide/gate), never
# on a bare mention of a person, and a construction inside a negated clause
# ("never ask the user", "no phase skill asks a person anything") is the
# policy being stated rather than a decision being routed.
# ===========================================================================
check_factory_unattended() {
  local found
  found=$(python3 - <<'PYEOF'
import pathlib, re, sys

# A person noun, never the possessive ("the user's repo") or a compound
# ("a user-facing change"): those are prose about people, not routing to one.
# "reviewer" and "author" are deliberately absent: in a code-review skill they
# name the downstream reader of a PR the run opens, not a gate inside the run.
PERSON = (r"(?:user|operator|human|maintainer|person|people|requester|requestor"
          r"|owner|stakeholder|someone|somebody|anyone|anybody|whoever|team"
          r"|them|they)s?\b(?![-'’])")
ARTICLE = r"(?:the\s+|a\s+|an\s+|each\s+|every\s+|any\s+|some\s+|another\s+|your\s+|their\s+)?"
ASK = (r"ask|prompt|poll|quer(?:y|ie)|consult|interview|question|survey|solicit"
       r"|check\s+with|check\s+in\s+with")
ROUTE = (r"confirm|check|verify|escalat\w*|surfac\w*|defer|hand(?:\s+off)?|rout\w*"
         r"|rais\w*|agree\w*|align|discuss|negotiat\w*|coordinat\w*|sync|refer"
         r"|bring|take|leave|let|flag|wait|paus\w*|loop\s+in|circle\s+back"
         r"|follow\s+up|report\s+back|put|send|forward|delegat\w*|punt|kick\s+up")
DECIDES = (r"decides?|decided|approves?|approved|authoriz\w+|chooses?|chose|confirms?"
           r"|answers?|asks?|says?|accepts?|accepted|picks?|selects?|responds?|replies"
           r"|weighs?\s+in|signs?\s+off|has\s+(?:explicitly\s+)?asked"
           r"|must\s+(?:decide|choose|answer|confirm|approve|accept|sign|say|pick|select)"
           r"|to\s+(?:decide|choose|confirm|approve|accept)")
GATE = (r"call|decision|choice|question|judgm?ent|approval|sign-?off|input|answer"
        r"|consent|permission|go-?ahead|say-?so|blessing|verdict|guidance|steer|gate")
PATTERN = re.compile(
    # 1. ask / prompt / consult a person
    rf"\b(?:{ASK})(?:s|es|ed|ing)?\s+{ARTICLE}{PERSON}"
    # 2. ask first, ask before doing it
    rf"|\bask(?:s|ed|ing)?\s+(?:first|before|again)\b"
    # 3. route it to / with / for a person, over a little filler
    rf"|\b(?:{ROUTE})(?:s|es|ed|ing)?\b(?:\s+\w+){{0,3}}?\s+(?:to|with|for|from|by|on)\s+{ARTICLE}{PERSON}"
    # 4. a person decides
    rf"|\b{ARTICLE}{PERSON}(?:\s+\w+){{0,4}}?\s+(?:{DECIDES})\b"
    # 5. a decision for / to / from a person
    rf"|\b(?:{GATE})s?\b(?![-'’])[^.]{{0,40}}?\b(?:for|to|from|by)\s+{ARTICLE}{PERSON}"
    # 6. a human call, manual approval, a user sign-off
    rf"|\b(?:human|manual|person|people|user|operator)[-\s](?:{GATE})s?\b(?![-'’])"
    # 7. their call, the user's say-so
    r"|\btheir\s+(?:call|say-?so|blessing|approval|sign-?off|consent|permission|go-?ahead)s?\b"
    rf"|\bthe\s+(?:user|human|person|operator|maintainer|owner|requester)'s\s+(?:{GATE})s?\b"
    # 8. get approval, await sign-off, seek permission
    r"|\b(?:get|obtain|seek|secure|request|await|need|require|ask\s+for)(?:s|ed|ing)?"
    r"\s+(?:\w+\s+){0,2}?(?:approval|sign-?off|confirmation|permission|consent"
    r"|the\s+go-?ahead|a\s+decision|an\s+answer)\b"
    # 9. wait for an answer
    r"|\bwait(?:s|ed|ing)?\s+(?:around\s+)?for\s+(?:\w+\s+){0,2}?"
    r"(?:answer|reply|response|approval|decision|confirmation|go-?ahead|sign-?off|input)s?\b"
    # 10. the structured user-input tools themselves
    r"|\bstructured user-input tool\b|\bAskUserQuestion\b",
    re.I,
)
# A negated clause states the policy ("never ask the user"); it routes nothing.
NEGATOR = re.compile(
    r"\b(?:never|not|no|none|nothing|neither|nor|without|instead\s+of|rather\s+than"
    r"|avoid(?:s|ed|ing)?|n't|cannot|can't)\b",
    re.I,
)
CLAUSE = re.compile(r"[.;:!?]|\s-\s")
OPEN, CLOSE = "<!-- interactive-only -->", "<!-- /interactive-only -->"
# Only the orchestrator has pre-go lines that legitimately reach a person.
MARKER_ALLOWED = ("factory/skills/run/SKILL.md",)
# The built-in pipeline's interactive phases keep their interview and sit outside
# the scan: D-go-placement puts every interactive phase before the go, so it has
# no post-go decision to route. Update this tuple with the built-in pipeline.
INTERACTIVE_PHASES = ("scope",)


def routes(line):
    """The first person-routing match on the line, ignoring negated clauses."""
    for match in PATTERN.finditer(line):
        prefix = line[: match.start()]
        bounds = [m.end() for m in CLAUSE.finditer(prefix)]
        clause = (prefix[bounds[-1]:] if bounds else prefix) + match.group(0)
        if not NEGATOR.search(clause):
            return match
    return None


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
    "surface it to the person and wait for the answer",
    "escalate to the requester",
    "hand it to the owner for a decision",
    "ask someone on the team",
    "check with them before proceeding",
    "this is a human decision",
    "get approval before merging",
    "seek sign-off from the maintainer",
    "the collaborator itself is a seam worth agreeing on with the user",
    "raise it with whoever owns the module",
    "in the end it is their call",
    "wait for their answer",
    "the requester decides",
    "a question for the owner",
    "ask first, then proceed",
    "pause for human input",
    "leave the call to the person running the factory",
    "defer to the stakeholder",
    "the owner must approve",
    "needs manual approval",
    "route the choice to a person",
    "the person running the run picks",
    "await confirmation",
    "the user's say-so",
    "let the maintainer choose",
    "put the question to the operator",
    "poll the team",
    "request permission first",
    "this is a judgment call for the user",
    "consult whoever owns it",
    "send it to the owner for sign-off",
    "ask them what they want",
    "take it up with the requester",
    "a decision that belongs to a person",
    "coordinate with the maintainer on the threshold",
    "delegate the call to a human",
    "the user says no PR",
    "interview the user",
    "the operator answers",
    "sync with the owner",
    "requires sign-off from a human",
    "a manual gate",
    "get the go-ahead from the owner",
)
NON_SAMPLES = (
    "the user's repository stays untouched",
    "a user-facing change needs an e2e scenario",
    "a whole user journey actually works",
    "no phase skill asks a person anything",
    "report failed naming what a person must supply",
    "never ask the user; decide under factory policy",
    "instead of asking the user, record it in auto_decided",
    "rather than escalating to a person, report failed with the reason",
    "the run never waits for an answer",
    "decide it without asking the user",
    "diffs whose owner did not ask for mutations",
    "the merge happens after their verdicts",
    "does it need a call-out to the reviewer",
    "name it so the reviewer can decide",
    "bring it to the author of the diff",
    "record the reason a person would need",
    "the exact step a person must take",
    "a user story per scenario",
    "the fix agent decides how",
    "user-visible behavior changes",
    "cannot ask the user, so it records auto_decided",
    "the owner of the module is the module itself",
)
for sample in SAMPLES:
    if not routes(sample):
        print(f"scripts/validate.sh: F03 pattern no longer matches {sample!r}")
        sys.exit(0)
for sample in NON_SAMPLES:
    if routes(sample):
        print(f"scripts/validate.sh: F03 pattern now falsely matches {sample!r}")
        sys.exit(0)

paths = []
phase_roots = [
    d for d in sorted(pathlib.Path("factory/phases").glob("*"))
    if d.is_dir() and d.name not in INTERACTIVE_PHASES
]
for root in phase_roots + [pathlib.Path("factory/skills/run")]:
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
    opened_at = None
    for number, line in body_lines(path):
        stripped = line.strip()
        # A marker only silences what follows when it stands alone on its line:
        # anything else on the line is prose, and prose is always scanned.
        if stripped not in (OPEN, CLOSE):
            if opened_at is None and routes(line):
                print(f"{path}:{number}: routes a decision to a person; decide under factory policy")
            continue
        if path.as_posix() not in MARKER_ALLOWED:
            print(f"{path}:{number}: interactive-only markers are allowed only in "
                  f"{', '.join(MARKER_ALLOWED)}; this file decides under factory policy instead")
            continue
        if stripped == OPEN:
            if opened_at is not None:
                print(f"{path}:{number}: interactive-only block opened again while the one "
                      f"opened at line {opened_at} is still open")
            opened_at = number
        else:
            if opened_at is None:
                print(f"{path}:{number}: interactive-only block closed but none was open")
            opened_at = None
    if opened_at is not None:
        print(f"{path}:{opened_at}: interactive-only block is never closed; every line "
              f"after it escapes the check")
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

BUILT_IN = ("scope", "scope-review", "build", "ship")
INVOKE = re.compile(r"\$factory:([a-z-]+)|/SKILL\.md\b")
RESULTS_PATH = "results/{phase}-{attempt}.json"
NEVER_LAUNCH = "Never name or launch the next phase."
MAX_LINES = 200


def frontmatter(text):
    """The frontmatter's key/value pairs, or None when the file has none."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    fields = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip()] = value.strip()
    return None


def body_rules(sf, phase):
    """The generic skill rules that reached these bodies while they lived under skills/."""
    text = sf.read_text(encoding="utf-8")
    total = len(text.splitlines())
    if total > MAX_LINES:
        print(f"{sf}: Body is {total} lines (recommend under 150; move detail to references/)")
    fields = frontmatter(text)
    if fields is None:
        print(f"{sf}: Does not start with a complete --- frontmatter block")
        return text
    if not fields.get("name") or not fields.get("description"):
        print(f"{sf}: Frontmatter 'name' or 'description' is empty or missing")
    if fields.get("name") and fields["name"] != phase:
        print(f"{sf}: Frontmatter name '{fields['name']}' != directory name '{phase}'")
    if fields.get("disable-model-invocation") != "true":
        print(f"{sf}: Frontmatter must set 'disable-model-invocation: true'")
    return text


phases_dir = pathlib.Path("factory/phases")
names = sorted(d.name for d in phases_dir.glob("*") if d.is_dir()) if phases_dir.is_dir() else []
for phase in BUILT_IN:
    if phase not in names:
        print(f"factory/phases/{phase}/SKILL.md: Factory phase skill not found")
for phase in names:
    sf = phases_dir / phase / "SKILL.md"
    if not sf.is_file():
        print(f"{phases_dir / phase}: Missing SKILL.md")
        continue
    text = body_rules(sf, phase)
    if "factory-run.json" not in text:
        print(f"{sf}: Must mention factory-run.json")
    if RESULTS_PATH not in text:
        print(f"{sf}: Must mention the literal {RESULTS_PATH}")
    if NEVER_LAUNCH not in text:
        print(f"{sf}: Must carry the sentence \"{NEVER_LAUNCH}\"")
    for match in INVOKE.finditer(text):
        skill = match.group(1)
        if skill is None or skill != phase:
            print(f"{sf}: invokes another phase ({match.group(0)}); a phase skill never launches another")

launch_md = pathlib.Path("factory/skills/run/references/launch.md")
if not launch_md.is_file():
    print(f"{launch_md}: launch wrapper not found")
else:
    launch_text = launch_md.read_text(encoding="utf-8")
    for literal in (RESULTS_PATH, "factory.result/1", NEVER_LAUNCH):
        if literal not in launch_text:
            print(f"{launch_md}: Must carry the literal {literal}")

run_md = pathlib.Path("factory/skills/run/SKILL.md")
if not run_md.is_file():
    print(f"{run_md}: run orchestrator skill not found")
    sys.exit(0)
if "run-state.py" not in run_md.read_text(encoding="utf-8"):
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
# B01: the bootstrap plugin's checker tests and concept-map tripwires pass
# ===========================================================================
check_bootstrap_script() {
  local dir="bootstrap/evals/tests"
  [ -d "$dir" ] || return
  if ! python3 - "$dir" >"$LOG_DIR/bootstrap-script-tests.log" 2>&1 <<'PYEOF'
import sys, unittest

loader = unittest.TestLoader()
suite = loader.discover(start_dir=sys.argv[1], top_level_dir=".")
result = unittest.TextTestRunner(verbosity=2).run(suite)
sys.exit(0 if result.wasSuccessful() and result.testsRun > 0 else 1)
PYEOF
  then
    tail -60 "$LOG_DIR/bootstrap-script-tests.log" >&2
    fail "B01" "$dir" \
      "Bootstrap script tests failed (full output: $LOG_DIR/bootstrap-script-tests.log); run: python3 -m unittest discover -s bootstrap/evals/tests -t ."
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
check_bootstrap_script

if [ -s "$ERROR_FILE" ]; then
  echo ""
  echo "=== FAILED ==="
  cat "$ERROR_FILE"
  exit 1
fi

echo ""
echo "All checks passed."
