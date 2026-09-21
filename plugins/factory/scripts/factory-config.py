#!/usr/bin/env python3
"""Read, write, and validate .factory/config.yaml: the declared pipeline.

Usage:
  factory-config.py init
  factory-config.py show [--resolved] [--json]
  factory-config.py check
  factory-config.py set <path> <value> [--item <value> ...]
  factory-config.py unset <path>

Exit codes:
  init    0 written; 1 the file already exists
  show    0 always (there is nothing to fail on a read)
  check   0 valid; 1 one or more findings, one per line; 3 a bad call
  set     0 recorded; 1 the phase id in the path does not exist;
          3 a malformed path or an unsupported document construct
  unset   0 recorded (a no-op if the key was already absent);
          3 a malformed path

3 always means the call itself could not be carried out - a malformed key
path, an unparseable document, or a usage error - never a finding about the
pipeline's own content. `check` is the single validator: every rule below is
enforced there and nowhere else, so the orchestrator's preflight only ever
relays `check`'s output rather than judging the file itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import NoReturn

BAD_CALL = 3
CONFIG_DIR = ".factory"
CONFIG_NAME = "config.yaml"
MANIFEST_NAME = ".inject.json"
HEADER = (
    "# .factory/config.yaml\n"
    "# Managed by factory-config.py. Edit with `factory-config.py set`.\n"
)
PLACEHOLDERS = ("${plugin_root}", "${repo_root}", "${plan_dir}", "${phase}", "${attempt}")
CHECK_PLACEHOLDERS = ("${plugin_root}", "${repo_root}", "${plan_dir}", "${phase}", "${attempt}")
EXECUTABLE_PLACEHOLDERS = ("${plugin_root}", "${repo_root}")
RESERVED_PHASE_IDS = {"defaults", "types"}

# ---------------------------------------------------------------------------
# The built-in default pipeline: today's four phases, equal to what
# judgment.md and the copied phase skills already enforce. Every path uses
# ${plugin_root} so a plugin move only ever touches this one constant.
# ---------------------------------------------------------------------------
BUILTIN_TYPES = {
    "interview": {
        "interactive": True,
        "requires": "the go was recorded (the interview happened inline in this session, "
        "so the orchestrator already knows)",
        "checks": [["${plugin_root}/skills/scope/scripts/lint-spec.py", "${plan_dir}/spec.md"]],
        "seals": ["${plan_dir}/spec.md"],
        "attempts": 3,
    },
    "review": {
        "interactive": False,
        "requires": 'spec-review_N.md contains a line whose whole text is exactly '
        '"Verdict: APPROVED" - nothing after it - with a "Rounds:" line, and the spec '
        'still lints clean with lint-spec.py',
        "attempts": 3,
    },
    "implement": {
        "interactive": False,
        "requires": "the spec's Validation block is green, and commits since handoff "
        "match the change plan's numbering",
        "checks": [["${plugin_root}/skills/build/scripts/check-tests.py", "${plan_dir}"]],
        "attempts": 3,
    },
    "ship": {
        "interactive": False,
        "requires": "review_N.md carries a verdict for HEAD and gh pr view shows the PR "
        "open with head equal to HEAD, or (without a GitHub remote) git ls-remote origin "
        "shows the branch at HEAD and pr.md is written",
        "checks": [["${plugin_root}/skills/ship/scripts/pr-evidence.py", "check", "${plan_dir}/pr.md"]],
        "attempts": 3,
    },
}
BUILTIN_PHASES = [
    {"id": "scope", "type": "interview", "skill": "${plugin_root}/skills/scope/SKILL.md"},
    {"id": "scope-review", "type": "review", "skill": "${plugin_root}/skills/scope-review/SKILL.md"},
    {"id": "build", "type": "implement", "skill": "${plugin_root}/skills/build/SKILL.md"},
    {"id": "ship", "type": "ship", "skill": "${plugin_root}/skills/ship/SKILL.md"},
]
BUILTIN_DEFAULTS = {"ceiling": 12}
DEFAULT_ATTEMPTS = 3

# ---------------------------------------------------------------------------
# A strict subset of YAML: block mappings, block sequences (including nested
# argv-list-of-lists for `checks`), plain and double-quoted scalars, and
# comments. Flow syntax, anchors, multiline block scalars, and single-quoted
# strings are rejected with ConfigError rather than silently mishandled.
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """An unsupported construct or a malformed document. Never a traceback."""


def _strip_comment(text: str) -> str:
    in_quotes = False
    escaped = False
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_quotes:
            escaped = True
            continue
        if ch == '"':
            in_quotes = not in_quotes
            continue
        if ch == "#" and not in_quotes and (i == 0 or text[i - 1].isspace()):
            return text[:i]
    return text


def _raw_lines(text: str) -> list:
    out = []
    for raw in text.splitlines():
        leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in leading:
            raise ConfigError("a tab in leading indentation")
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip(" "))
        if indent % 2 != 0:
            raise ConfigError(f"odd indentation ({indent} spaces): {stripped.strip()!r}")
        out.append((indent, stripped.strip()))
    return out


def _unfold(indent: int, content: str) -> list:
    if not (content == "-" or content.startswith("- ")):
        return [(indent, content, False)]
    out = []
    while True:
        rest = "" if content == "-" else content[2:]
        if rest == "":
            out.append((indent, None, True))
            return out
        if rest == "-" or rest.startswith("- "):
            out.append((indent, None, True))
            indent += 2
            content = rest
            continue
        out.append((indent, rest, True))
        return out


def _tokenize(text: str) -> list:
    entries = []
    for indent, content in _raw_lines(text):
        entries.extend(_unfold(indent, content))
    return entries


def _parse_scalar(text: str):
    text = text.strip()
    if text == "":
        return ""
    if text == "[]":
        return []
    if text == "{}":
        return {}
    if text.startswith("["):
        raise ConfigError(f"flow sequence: {text!r}")
    if text.startswith("{"):
        raise ConfigError(f"flow mapping: {text!r}")
    if text in ("|", ">") or (len(text) == 2 and text[0] in "|>" and text[1] in "+-"):
        raise ConfigError(f"multiline scalar block: {text!r}")
    if text[0] in "&*":
        raise ConfigError(f"anchor or alias: {text!r}")
    if text.startswith('"'):
        if not text.endswith('"') or len(text) < 2:
            raise ConfigError(f"unterminated double-quoted scalar: {text!r}")
        body = text[1:-1]
        out = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "\\" and i + 1 < len(body):
                nxt = body[i + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    if text.startswith("'"):
        raise ConfigError(f"single-quoted scalar: {text!r}")
    if text == "true":
        return True
    if text == "false":
        return False
    if text in ("null", "~"):
        return None
    try:
        return int(text)
    except ValueError:
        pass
    return text


def _parse_mapping(entries, i, indent):
    result = {}
    while i < len(entries) and entries[i][0] == indent and not entries[i][2]:
        _, content, _ = entries[i]
        key, sep, rest = content.partition(":")
        if not sep:
            raise ConfigError(f"expected 'key: value' or 'key:', found {content!r}")
        key = key.strip()
        rest = rest.strip()
        i += 1
        if rest == "":
            if i < len(entries) and entries[i][0] > indent:
                value, i = _parse_node(entries, i, entries[i][0])
            else:
                value = None
        else:
            value = _parse_scalar(rest)
        result[key] = value
    return result, i


def _parse_sequence(entries, i, indent):
    items = []
    while i < len(entries) and entries[i][0] == indent and entries[i][2]:
        _, content, _ = entries[i]
        i += 1
        if content is None:
            if i < len(entries) and entries[i][0] > indent:
                value, i = _parse_node(entries, i, entries[i][0])
            else:
                raise ConfigError("sequence item has no value")
        elif ":" in content and not content.startswith('"'):
            item_indent = indent + 2
            synthetic = [(item_indent, content, False)] + entries[i:]
            value, consumed = _parse_mapping(synthetic, 0, item_indent)
            i += consumed - 1
        else:
            value = _parse_scalar(content)
        items.append(value)
    return items, i


def _parse_node(entries, i, indent):
    if i >= len(entries) or entries[i][0] != indent:
        raise ConfigError(f"expected content at indent {indent}")
    if entries[i][2]:
        return _parse_sequence(entries, i, indent)
    return _parse_mapping(entries, i, indent)


def parse_config(text: str) -> dict:
    entries = _tokenize(text)
    if not entries:
        return {}
    value, next_i = _parse_node(entries, 0, entries[0][0])
    if next_i != len(entries):
        raise ConfigError(f"unexpected indentation back to {entries[next_i][0]} after the document")
    if not isinstance(value, dict):
        raise ConfigError("document root is not a mapping")
    return value


_RESERVED_SCALARS = {"true", "false", "null", "~", ""}


def _needs_quotes(text: str) -> bool:
    if text in _RESERVED_SCALARS:
        return True
    if text != text.strip():
        return True
    if ": " in text or text.endswith(":"):
        return True
    if text[0] in "\"'[]{}&*|>#-?:@`":
        return True
    try:
        int(text)
        return True
    except ValueError:
        pass
    return False


def _dump_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if value is None:
        return "null"
    text = str(value)
    if _needs_quotes(text):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return text


def _dump_node(value, indent: int, lines: list) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{pad}{key}:")
                _dump_node(item, indent + 2, lines)
            elif isinstance(item, dict):
                lines.append(f"{pad}{key}: {{}}")
            elif isinstance(item, list):
                lines.append(f"{pad}{key}: []")
            else:
                lines.append(f"{pad}{key}: {_dump_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                keys = list(item.items())
                first_key, first_val = keys[0]
                if isinstance(first_val, (dict, list)) and first_val:
                    lines.append(f"{pad}- {first_key}:")
                    _dump_node(first_val, indent + 4, lines)
                else:
                    lines.append(f"{pad}- {first_key}: {_dump_scalar(first_val)}")
                for key, val in keys[1:]:
                    if isinstance(val, (dict, list)) and val:
                        lines.append(f"{pad}  {key}:")
                        _dump_node(val, indent + 4, lines)
                    else:
                        lines.append(f"{pad}  {key}: {_dump_scalar(val)}")
            elif isinstance(item, list):
                if not item:
                    lines.append(f"{pad}- []")
                    continue
                first = item[0]
                if isinstance(first, list):
                    raise ConfigError("triply-nested list is outside the supported subset")
                lines.append(f"{pad}- - {_dump_scalar(first)}")
                for element in item[1:]:
                    lines.append(f"{pad}  - {_dump_scalar(element)}")
            else:
                lines.append(f"{pad}- {_dump_scalar(item)}")


def dump_config(doc: dict) -> str:
    lines: list = []
    _dump_node(doc, 0, lines)
    return "\n".join(lines) + "\n" if lines else ""


# ---------------------------------------------------------------------------
# File location and loading.
# ---------------------------------------------------------------------------


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR / CONFIG_NAME


def manifest_path(repo_root: Path) -> Path:
    return repo_root / CONFIG_DIR / MANIFEST_NAME


def load_raw_doc(repo_root: Path):
    """Return (doc, error) - doc is None with a config file present but unparseable."""
    path = config_path(repo_root)
    if not path.exists():
        return None, None
    try:
        return parse_config(path.read_text()), None
    except ConfigError as exc:
        return None, str(exc)


def builtin_doc() -> dict:
    return {
        "version": 1,
        "defaults": dict(BUILTIN_DEFAULTS),
        "types": {name: dict(t) for name, t in BUILTIN_TYPES.items()},
        "phases": [dict(p) for p in BUILTIN_PHASES],
    }


def effective_doc(repo_root: Path):
    """Return (doc, using_builtin, error)."""
    raw, error = load_raw_doc(repo_root)
    if error is not None:
        return None, False, error
    if raw is None:
        return builtin_doc(), True, None
    return raw, False, None


# ---------------------------------------------------------------------------
# Placeholder substitution.
# ---------------------------------------------------------------------------


def substitute(text: str, context: dict) -> str:
    if not isinstance(text, str):
        return text
    for name in PLACEHOLDERS:
        key = name[2:-1]
        if key in context and name in text:
            text = text.replace(name, str(context[key]))
    return text


def placeholders_in(text: str) -> list:
    if not isinstance(text, str):
        return []
    found = []
    i = 0
    while True:
        i = text.find("${", i)
        if i == -1:
            break
        j = text.find("}", i)
        if j == -1:
            break
        found.append(text[i : j + 1])
        i = j + 1
    return found


# ---------------------------------------------------------------------------
# Resolution: phase list with type axes merged in, checks/attempts precedence
# applied, and paths substituted.
# ---------------------------------------------------------------------------


class ResolutionError(Exception):
    def __init__(self, findings):
        super().__init__("; ".join(findings))
        self.findings = list(findings)


def _normalize_under_root(raw: str, roots: dict) -> str:
    """Substitute ${plugin_root}/${repo_root} and normalize .. segments."""
    text = raw
    for name in ("${plugin_root}", "${repo_root}"):
        key = name[2:-1]
        if key in roots:
            text = text.replace(name, str(roots[key]))
    return text


def _resolved_path(path_text: str, roots: dict):
    """The absolute path a skill or check executable names, or None when unresolvable."""
    resolved = Path(_normalize_under_root(path_text, roots))
    try:
        if not resolved.is_absolute():
            resolved = Path(roots["repo_root"]) / resolved
        return resolved.resolve()
    except (OSError, RuntimeError):
        return None


def _resolves_under_allowlist(path_text: str, roots: dict) -> bool:
    resolved = _resolved_path(path_text, roots)
    if resolved is None:
        return False
    for root_key in ("plugin_root", "repo_root"):
        try:
            resolved.relative_to(Path(roots[root_key]).resolve())
            return True
        except ValueError:
            continue
    return False


def _path_findings(pid: str, what: str, path_text: str, roots: dict) -> list:
    """Findings for a skill path or check executable outside the roots or not a file."""
    if not _resolves_under_allowlist(path_text, roots):
        return [f"phase {pid!r}: {what} {path_text!r} is outside the plugin and repo roots"]
    resolved = _resolved_path(path_text, roots)
    if resolved is None or not resolved.is_file():
        return [f"phase {pid!r}: {what} {path_text!r} does not resolve to a file"]
    return []


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def classify_phase(phase: dict, roots: dict, repo_root: Path):
    """Return (class_name, findings) where class_name in built-in/injected/foreign."""
    skill = phase.get("skill", "")
    findings = []
    if isinstance(skill, str) and skill.startswith("${plugin_root}"):
        return "built-in", findings

    factory_dir = repo_root / CONFIG_DIR
    resolved_skill = Path(_normalize_under_root(skill, roots)) if isinstance(skill, str) else None
    if resolved_skill is not None and not resolved_skill.is_absolute():
        resolved_skill = repo_root / resolved_skill

    under_factory_skills = False
    if resolved_skill is not None:
        try:
            resolved_skill.resolve().relative_to((factory_dir / "skills").resolve())
            under_factory_skills = True
        except (ValueError, OSError):
            under_factory_skills = False

    if not under_factory_skills:
        return "foreign", findings

    manifest_file = manifest_path(repo_root)
    if not manifest_file.exists():
        findings.append(
            f"phase {phase.get('id', '?')}: skill under .factory/skills/ but .factory/.inject.json is missing"
        )
        return "foreign", findings
    try:
        manifest = json.loads(manifest_file.read_text())
    except (json.JSONDecodeError, OSError):
        findings.append(
            f"phase {phase.get('id', '?')}: .factory/.inject.json is unparseable"
        )
        return "foreign", findings

    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    try:
        rel = resolved_skill.resolve().relative_to(factory_dir.resolve())
    except (ValueError, OSError):
        findings.append(f"phase {phase.get('id', '?')}: skill path could not be related to .factory/")
        return "foreign", findings
    rel_str = str(rel)
    entry_hash = files.get(rel_str)
    if entry_hash is None:
        findings.append(
            f"phase {phase.get('id', '?')}: {rel_str} has no entry in .factory/.inject.json"
        )
        return "foreign", findings
    if not resolved_skill.exists():
        findings.append(f"phase {phase.get('id', '?')}: {rel_str} does not exist")
        return "foreign", findings
    if sha256_of(resolved_skill) != entry_hash:
        findings.append(
            f"phase {phase.get('id', '?')}: {rel_str} does not match its .factory/.inject.json entry"
        )
        return "foreign", findings
    return "injected", findings


def resolve(repo_root: Path, plugin_root_path: Path = None):
    """Return (resolved, findings). resolved is None when findings is non-empty."""
    doc, using_builtin, parse_error = effective_doc(repo_root)
    findings = []
    if parse_error is not None:
        findings.append(f"malformed document: {parse_error}")
        return None, findings

    proot = plugin_root_path or plugin_root()
    roots = {
        "plugin_root": str(proot),
        "repo_root": str(repo_root),
    }

    version = doc.get("version")
    if version != 1:
        findings.append(f"unsupported version: {version!r}")

    defaults = doc.get("defaults") or {}
    types = doc.get("types") or {}
    phases = doc.get("phases") or []

    if not isinstance(phases, list) or not phases:
        findings.append("empty phase list")
        return None, findings

    seen_ids = set()
    for phase in phases:
        if not isinstance(phase, dict):
            findings.append(f"malformed phase entry: {phase!r}")
            continue
        pid = phase.get("id")
        if pid in RESERVED_PHASE_IDS:
            findings.append(f"phase id {pid!r} is reserved")
        if pid in seen_ids:
            findings.append(f"duplicate phase id: {pid!r}")
        seen_ids.add(pid)
        ptype = phase.get("type")
        if ptype not in types and not (using_builtin and ptype in BUILTIN_TYPES):
            findings.append(f"phase {pid!r} declares undeclared type {ptype!r}")

    if findings:
        return None, findings

    resolved_phases = []
    ceiling = defaults.get("ceiling", BUILTIN_DEFAULTS["ceiling"])

    for phase in phases:
        pid = phase["id"]
        ptype = phase["type"]
        type_def = types.get(ptype, BUILTIN_TYPES.get(ptype, {}))

        skill_raw = phase.get("skill", "")
        findings.extend(_path_findings(pid, "skill path", skill_raw, roots))
        skill_resolved = substitute(skill_raw, {"plugin_root": roots["plugin_root"], "repo_root": roots["repo_root"]})

        checks_raw = phase["checks"] if "checks" in phase else type_def.get("checks", [])
        if not isinstance(checks_raw, list):
            findings.append(f"phase {pid!r}: checks must be a list of argv lists")
            checks_raw = []
        resolved_checks = []
        for entry in checks_raw:
            if not isinstance(entry, list) or not entry or not all(isinstance(e, str) for e in entry):
                findings.append(f"phase {pid!r}: a checks entry must be an argv list of strings, not a shell string")
                continue
            executable = entry[0]
            for ph in placeholders_in(executable):
                if ph not in EXECUTABLE_PLACEHOLDERS:
                    findings.append(
                        f"phase {pid!r}: check executable {executable!r} uses placeholder {ph}, "
                        f"only {EXECUTABLE_PLACEHOLDERS} are allowed there"
                    )
            for element in entry:
                for ph in placeholders_in(element):
                    if ph not in CHECK_PLACEHOLDERS:
                        findings.append(f"phase {pid!r}: check {entry!r} uses unsupported placeholder {ph}")
            findings.extend(_path_findings(pid, "check executable", executable, roots))
            resolved_entry = [substitute(e, {"plugin_root": roots["plugin_root"], "repo_root": roots["repo_root"]}) for e in entry]
            resolved_checks.append(resolved_entry)

        seals_raw = type_def.get("seals", [])
        resolved_seals = list(seals_raw)

        attempts = phase.get("attempts", type_def.get("attempts", defaults.get("attempts", DEFAULT_ATTEMPTS)))

        phase_class, class_findings = classify_phase(phase, roots, repo_root)
        findings.extend(class_findings)
        unattended_safe = phase.get("unattended_safe", False)
        if phase_class == "foreign" and not unattended_safe:
            findings.append(f"phase {pid!r} is foreign and lacks unattended_safe")

        resolved_phases.append(
            {
                "id": pid,
                "type": ptype,
                "class": phase_class,
                "interactive": bool(type_def.get("interactive", False)),
                "skill": skill_resolved,
                "checks": resolved_checks,
                "requires": type_def.get("requires", ""),
                "seals": resolved_seals,
                "attempts": attempts,
                "unattended_safe": unattended_safe,
            }
        )

    seen_unattended = False
    for i, rp in enumerate(resolved_phases):
        if rp["interactive"]:
            if seen_unattended:
                prev = next(p for p in resolved_phases[:i] if not p["interactive"])
                findings.append(
                    f"interactive phase {rp['id']!r} declared after unattended phase {prev['id']!r}"
                )
        else:
            seen_unattended = True

    go_after = None
    for rp in resolved_phases:
        if rp["interactive"]:
            go_after = rp["id"]
        else:
            break

    if findings:
        return None, findings

    return {"phases": resolved_phases, "ceiling": ceiling, "go_after": go_after, "using_builtin": using_builtin}, []


# ---------------------------------------------------------------------------
# Writing: schema key order, two-space indent (dump_config's default),
# block style only, one trailing newline, a regenerated header comment.
# ---------------------------------------------------------------------------

SCHEMA_KEY_ORDER = ("version", "defaults", "types", "phases")


def write_doc(repo_root: Path, doc: dict) -> None:
    top = {key: doc[key] for key in SCHEMA_KEY_ORDER if key in doc}
    text = HEADER + dump_config(top)
    path = config_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def read_doc_for_write(repo_root: Path):
    """Load the raw file for a set/unset call, defaulting to an empty shell."""
    raw, error = load_raw_doc(repo_root)
    if error is not None:
        raise ConfigError(error)
    if raw is None:
        raw = {"version": 1, "defaults": {}, "types": {}, "phases": []}
    raw.setdefault("version", 1)
    raw.setdefault("defaults", {})
    raw.setdefault("types", {})
    raw.setdefault("phases", [])
    return raw


# ---------------------------------------------------------------------------
# show --resolved rendering.
# ---------------------------------------------------------------------------


def render_human(resolved: dict) -> str:
    lines = []
    source = "built-in default" if resolved["using_builtin"] else ".factory/config.yaml"
    lines.append(f"pipeline: {source}")
    lines.append(f"ceiling: {resolved['ceiling']}")
    lines.append(f"go: {'after ' + resolved['go_after'] if resolved['go_after'] else 'none'}")
    for phase in resolved["phases"]:
        lines.append(f"- {phase['id']} ({phase['type']}, {phase['class']})")
        lines.append(f"  interactive: {phase['interactive']}")
        lines.append(f"  skill: {phase['skill']}")
        lines.append(f"  attempts: {phase['attempts']}")
        if phase["requires"]:
            lines.append(f"  requires: {phase['requires']}")
        for entry in phase["checks"]:
            lines.append(f"  check: {entry}")
        for seal in phase["seals"]:
            lines.append(f"  seal: {seal}")
        if phase["unattended_safe"]:
            lines.append("  unattended_safe: true")
    return "\n".join(lines) + "\n"


def render_json(resolved: dict) -> str:
    phases = [
        {"id": p["id"], "type": p["type"], "skill": p["skill"], "interactive": p["interactive"]}
        for p in resolved["phases"]
    ]
    return json.dumps({"phases": phases}, indent=2) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        sys.stderr.write(f"{self.prog}: {message}\n")
        raise SystemExit(BAD_CALL)


def build_parser() -> Parser:
    parser = Parser(prog="factory-config.py", description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--plugin-root", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init")

    p_show = sub.add_parser("show")
    p_show.add_argument("--resolved", action="store_true")
    p_show.add_argument("--json", action="store_true")

    sub.add_parser("check")

    p_set = sub.add_parser("set")
    p_set.add_argument("path")
    p_set.add_argument("value", nargs="?")
    p_set.add_argument("--item", action="append", default=None)

    p_unset = sub.add_parser("unset")
    p_unset.add_argument("path")

    p_inject = sub.add_parser("inject")
    p_inject.add_argument("phases", nargs="*")
    p_inject.add_argument("--force", action="store_true")

    return parser


def cmd_init(args) -> int:
    path = config_path(args.repo_root)
    if path.exists():
        print(f"{path} already exists", file=sys.stderr)
        return 1
    write_doc(args.repo_root, {"version": 1, "defaults": {}, "types": {}, "phases": []})
    return 0


def cmd_show(args) -> int:
    resolved, findings = resolve(args.repo_root, args.plugin_root)
    if findings:
        for f in findings:
            print(f, file=sys.stderr)
        return 1
    if args.json:
        print(render_json(resolved), end="")
    else:
        print(render_human(resolved), end="")
    return 0


def cmd_check(args) -> int:
    resolved, findings = resolve(args.repo_root, args.plugin_root)
    if findings:
        for f in findings:
            print(f)
        return 1
    return 0


def _parse_path(path: str):
    """Return (head, key) for <phase-id>.<key>, defaults.<key> or types.<name>.<axis>."""
    if "." not in path:
        return None
    head, rest = path.split(".", 1)
    if head == "types":
        if "." not in rest:
            return None
        name, axis = rest.split(".", 1)
        return ("types", name, axis)
    return (head, rest)


def _coerce_cli_value(text):
    if text is None:
        return None
    if text == "true":
        return True
    if text == "false":
        return False
    try:
        return int(text)
    except ValueError:
        return text


def cmd_set(args) -> int:
    parsed = _parse_path(args.path)
    if parsed is None:
        print(f"malformed path: {args.path!r}", file=sys.stderr)
        return BAD_CALL

    try:
        doc = read_doc_for_write(args.repo_root)
    except ConfigError as exc:
        print(f"malformed document: {exc}", file=sys.stderr)
        return BAD_CALL

    if args.item:
        key_name = parsed[-1]
        if key_name == "checks":
            value = [list(args.item)]
        else:
            value = list(args.item)
    else:
        value = _coerce_cli_value(args.value)

    if len(parsed) == 3:
        _, name, axis = parsed
        doc["types"].setdefault(name, {})[axis] = value
    elif parsed[0] == "defaults":
        _, key = parsed
        doc["defaults"][key] = value
    else:
        head, key = parsed
        phase = next((p for p in doc["phases"] if p.get("id") == head), None)
        if phase is None:
            print(f"no phase with id {head!r}", file=sys.stderr)
            return 1
        phase[key] = value

    write_doc(args.repo_root, doc)
    return 0


def cmd_unset(args) -> int:
    parsed = _parse_path(args.path)
    if parsed is None:
        print(f"malformed path: {args.path!r}", file=sys.stderr)
        return BAD_CALL

    try:
        doc = read_doc_for_write(args.repo_root)
    except ConfigError as exc:
        print(f"malformed document: {exc}", file=sys.stderr)
        return BAD_CALL

    if len(parsed) == 3:
        _, name, axis = parsed
        doc["types"].get(name, {}).pop(axis, None)
    elif parsed[0] == "defaults":
        _, key = parsed
        doc["defaults"].pop(key, None)
    else:
        head, key = parsed
        phase = next((p for p in doc["phases"] if p.get("id") == head), None)
        if phase is not None:
            phase.pop(key, None)
            if not phase:
                doc["phases"].remove(phase)

    write_doc(args.repo_root, doc)
    return 0


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.plugin_root is None:
        args.plugin_root = plugin_root()
    handlers = {
        "init": cmd_init,
        "show": cmd_show,
        "check": cmd_check,
        "set": cmd_set,
        "unset": cmd_unset,
    }
    handler = handlers.get(args.command)
    if handler is None:
        print(f"'{args.command}' is not yet implemented", file=sys.stderr)
        return BAD_CALL
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
