#!/usr/bin/env python3
"""Enforce a repository's architecture from one human-owned file, docs/architecture.toml.

Usage: python3 arch.py <command> [--root DIR] [--file PATH]

  validate   the file is well formed, edges point down the layer order, and every
             rule and its D- decision in docs/decisions.md name each other
  generate   write every tool config under .arch/ from the file, plus a manifest
  check      validate, generated files untouched, the baseline only shrinks
             (--base REF compares against git; --allow-loosening accepts a wider
             rule set), and run the built-in checks (Python and Rust dependency
             allowlists, Rust crate edges, C++ includes)
  run        check, then run each generated tool and ast-grep's fixture tests;
             a failure prints the remediation of each rule it names
  redteam    plant one violation per rule in place, run the tools, and fail on any
             rule that stays green; every planted file is removed afterwards
  infer      print the import graph between candidate modules (--language L,
             --depth N) and a draft [[modules]] block to start the file from

Generated files are build artifacts: never edit them, change the toml and run
generate. Standard library only, so it runs in any repository's CI: tomllib on Python
3.11+, tomli when installed, else a built-in reader for the TOML this file and Cargo
manifests use.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

VERSION = 1
SOURCE = "docs/architecture.toml"
OUT = ".arch"
LANGUAGES = ("python", "typescript", "go", "jvm", "rust", "cpp")
# ast-grep language -> the module language whose files it scans
AST_GREP_LANGUAGES = {
    "python": "python", "typescript": "typescript", "tsx": "typescript", "javascript": "typescript",
    "go": "go", "java": "jvm", "kotlin": "jvm", "rust": "rust", "cpp": "cpp", "c": "cpp",
}
EXTENSIONS = {
    "python": (".py",), "typescript": (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"), "go": (".go",),
    "jvm": (".java", ".kt"), "rust": (".rs",), "cpp": (".h", ".hh", ".hpp", ".c", ".cc", ".cpp", ".cxx"),
}
PROBE_EXTENSION = {
    "python": ".py", "typescript": ".ts", "tsx": ".tsx", "javascript": ".js", "go": ".go",
    "java": ".java", "kotlin": ".kt", "rust": ".rs", "cpp": ".cpp", "c": ".c",
}
DEFAULT_TOOLS = {
    "python": "lint-imports --config .arch/importlinter.ini",
    "typescript": "npx depcruise --config .arch/dependency-cruiser.cjs --ignore-known .arch/dependency-cruiser-known.json {paths}",
    "go": "go-arch-lint check --arch-file .arch/go-arch-lint.yml",
    "jvm": "./gradlew test --tests *ArchitectureTest",
    "ast-grep": "ast-grep scan --config .arch/sgconfig.yml --error",
    "ast-grep-test": "ast-grep test --config .arch/sgconfig.yml --skip-snapshot-tests",
}
PROBE = "arch_redteam_probe"
SLUG = re.compile(r"^(D-[a-z0-9]+(?:-[a-z0-9]+)*):")
ENFORCED = re.compile(r"^\s+enforced by:\s*(.+)$")
RULE_ID = re.compile(r"^[a-z0-9]+(?:[-:][a-z0-9]+)*$")


class ArchError(Exception):
    """A problem with the file itself; the message says how to fix it."""


class TOMLDecodeError(ValueError):
    pass


def _parse_toml(text: str) -> dict:
    """The TOML subset architecture files and Cargo manifests use, for Pythons without tomllib.

    Tables, arrays of tables, dotted keys, basic and literal strings, integers, floats,
    booleans, and arrays and inline tables of those, across lines; no dates, no multi-line strings.
    """
    root: dict = {}
    current = root
    pos, n = 0, len(text)

    def fail(message: str):
        line = text.count("\n", 0, pos) + 1
        raise TOMLDecodeError(f"{message} (line {line})")

    def skip(newlines: bool) -> None:
        nonlocal pos
        while pos < n:
            ch = text[pos]
            if ch in " \t\r" or (newlines and ch == "\n"):
                pos += 1
            elif ch == "#":
                while pos < n and text[pos] != "\n":
                    pos += 1
            else:
                return

    def key() -> list[str]:
        nonlocal pos
        parts = []
        while True:
            skip(False)
            if pos < n and text[pos] in "\"'":
                parts.append(string())
            else:
                match = re.compile(r"[A-Za-z0-9_-]+").match(text, pos)
                if not match:
                    fail("expected a key")
                parts.append(match.group(0))
                pos = match.end()
            skip(False)
            if pos < n and text[pos] == ".":
                pos += 1
                continue
            return parts

    def string() -> str:
        nonlocal pos
        quote = text[pos]
        end = pos + 1
        while end < n and text[end] != quote:
            if text[end] == "\n":
                fail("unterminated string")
            end += 2 if quote == '"' and text[end] == "\\" else 1
        if end >= n:
            fail("unterminated string")
        raw = text[pos:end + 1]
        pos = end + 1
        return json.loads(raw) if quote == '"' else raw[1:-1]

    def value():
        nonlocal pos
        skip(False)
        if pos >= n:
            fail("expected a value")
        ch = text[pos]
        if ch in "\"'":
            if text.startswith(ch * 3, pos):
                fail("multi-line strings need Python 3.11+ or tomli")
            return string()
        if ch == "[":
            pos += 1
            items = []
            while True:
                skip(True)
                if pos < n and text[pos] == "]":
                    pos += 1
                    return items
                items.append(value())
                skip(True)
                if pos < n and text[pos] == ",":
                    pos += 1
                elif pos < n and text[pos] == "]":
                    pos += 1
                    return items
                else:
                    fail("expected , or ] in an array")
        if ch == "{":
            pos += 1
            table: dict = {}
            skip(False)
            if pos < n and text[pos] == "}":
                pos += 1
                return table
            while True:
                path = key()
                if pos >= n or text[pos] != "=":
                    fail("expected = in an inline table")
                pos += 1
                assign(table, path, value())
                skip(False)
                if pos < n and text[pos] == ",":
                    pos += 1
                elif pos < n and text[pos] == "}":
                    pos += 1
                    return table
                else:
                    fail("expected , or } in an inline table")
        match = re.compile(r"true|false|[+-]?(?:\d[\d_]*)(?:\.\d+)?(?:[eE][+-]?\d+)?").match(text, pos)
        if not match:
            fail("unsupported value; this reader covers strings, numbers, booleans, arrays, and inline tables")
        pos = match.end()
        word = match.group(0)
        if word in ("true", "false"):
            return word == "true"
        number = word.replace("_", "")
        return float(number) if any(c in number for c in ".eE") else int(number)

    def descend(table: dict, path: list[str]) -> dict:
        for part in path:
            table = table.setdefault(part, {})
            if isinstance(table, list):
                table = table[-1]
            if not isinstance(table, dict):
                fail(f"{part} is not a table")
        return table

    def assign(table: dict, path: list[str], item) -> None:
        target = descend(table, path[:-1])
        if path[-1] in target:
            fail(f"{'.'.join(path)} is defined twice")
        target[path[-1]] = item

    while True:
        skip(True)
        if pos >= n:
            return root
        if text.startswith("[[", pos):
            pos += 2
            path = key()
            if not text.startswith("]]", pos):
                fail("expected ]]")
            pos += 2
            parent = descend(root, path[:-1])
            array = parent.setdefault(path[-1], [])
            if not isinstance(array, list):
                fail(f"{'.'.join(path)} is not an array of tables")
            array.append({})
            current = array[-1]
        elif text[pos] == "[":
            pos += 1
            path = key()
            if pos >= n or text[pos] != "]":
                fail("expected ]")
            pos += 1
            current = descend(root, path)
        else:
            path = key()
            if pos >= n or text[pos] != "=":
                fail("expected =")
            pos += 1
            assign(current, path, value())
        skip(False)
        if pos < n and text[pos] != "\n":
            fail("expected the end of the line")


try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None  # type: ignore[assignment]


def toml_loads(text: str) -> dict:
    if tomllib is not None and not os.environ.get("ARCH_TOML_FALLBACK"):
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise TOMLDecodeError(str(error)) from None
    return _parse_toml(text)


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------


@dataclass
class Module:
    name: str
    layer: str
    language: str
    path: str
    may_depend_on: list[str]
    public: list[str]
    decision: str
    instead: str
    package: str = ""  # jvm only


@dataclass
class Banned:
    id: str
    language: str
    pattern: str
    message: str
    instead: str
    decision: str
    allowed_in: list[str]
    invalid: list[str]
    valid: list[str]


@dataclass
class Allowlist:
    language: str
    allow: list[str]
    decision: str
    instead: str

    @property
    def id(self) -> str:
        return f"dependencies:{self.language}"


@dataclass
class Rule:
    id: str
    decision: str
    message: str  # the remediation an agent reads, rule id first


@dataclass
class Architecture:
    root: Path
    raw: dict
    languages: list[str]
    layers: list[str]
    modules: list[Module]
    banned: list[Banned]
    allowlists: list[Allowlist]
    baseline: list[dict]
    providers: dict | None
    settings: dict
    tools: dict
    decisions_path: str
    rules: dict[str, Rule] = field(default_factory=dict)

    def module(self, name: str) -> Module:
        return next(m for m in self.modules if m.name == name)

    def in_language(self, language: str) -> list[Module]:
        return [m for m in self.modules if m.language == language]

    def setting(self, language: str, key: str, default=None):
        return self.settings.get(language, {}).get(key, default)

    def baseline_for(self, rule: str) -> list[dict]:
        return [b for b in self.baseline if b["rule"] == rule]


def need(table: dict, key: str, kind: type, where: str):
    if key not in table:
        raise ArchError(f"{where} needs '{key}'")
    value = table[key]
    if not isinstance(value, kind):
        raise ArchError(f"{where}: '{key}' must be a {kind.__name__}")
    return value


def strings(table: dict, key: str, where: str) -> list[str]:
    value = table.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ArchError(f"{where}: '{key}' must be a list of strings")
    return value


def load(root: Path, source: str = SOURCE) -> Architecture:
    path = root / source
    if not path.is_file():
        raise ArchError(f"no {source}; run the dev bootstrap skill to write one")
    try:
        raw = toml_loads(path.read_text(encoding="utf-8"))
    except TOMLDecodeError as error:
        raise ArchError(f"{source} is not valid TOML: {error}") from None
    if raw.get("version") != VERSION:
        raise ArchError(f"{source}: version must be {VERSION}")
    languages = strings(raw, "languages", source)
    for language in languages:
        if language not in LANGUAGES:
            raise ArchError(f"{source}: unknown language '{language}'; use one of {', '.join(LANGUAGES)}")
    if not languages:
        raise ArchError(f"{source}: 'languages' lists none")
    layers = strings(raw, "layers", source)
    modules = []
    for index, table in enumerate(raw.get("modules", [])):
        where = f"[[modules]] #{index + 1}"
        name = need(table, "name", str, where)
        where = f"module {name}"
        language = table.get("language", languages[0] if len(languages) == 1 else None)
        if language not in languages:
            raise ArchError(f"{where}: 'language' must be one of the file's languages")
        modules.append(Module(
            name=name, layer=need(table, "layer", str, where), language=language,
            path=need(table, "path", str, where).rstrip("/"),
            may_depend_on=strings(table, "may_depend_on", where), public=strings(table, "public", where),
            decision=need(table, "decision", str, where), instead=need(table, "instead", str, where),
            package=table.get("package", ""),
        ))
    banned = []
    for index, table in enumerate(raw.get("banned", [])):
        where = f"[[banned]] #{index + 1}"
        rule_id = need(table, "id", str, where)
        where = f"banned {rule_id}"
        banned.append(Banned(
            id=rule_id, language=need(table, "language", str, where), pattern=need(table, "pattern", str, where),
            message=need(table, "message", str, where), instead=need(table, "instead", str, where),
            decision=need(table, "decision", str, where), allowed_in=strings(table, "allowed_in", where),
            invalid=strings(table, "invalid", where), valid=strings(table, "valid", where),
        ))
    allowlists = []
    for index, table in enumerate(raw.get("dependencies", [])):
        where = f"[[dependencies]] #{index + 1}"
        allowlists.append(Allowlist(
            language=need(table, "language", str, where), allow=strings(table, "allow", where),
            decision=need(table, "decision", str, where), instead=need(table, "instead", str, where),
        ))
    baseline = []
    for index, table in enumerate(raw.get("baseline", [])):
        where = f"[[baseline]] #{index + 1}"
        baseline.append({key: need(table, key, str, where) for key in ("rule", "from", "to", "reason")})
    arch = Architecture(
        root=root, raw=raw, languages=languages, layers=layers, modules=modules, banned=banned,
        allowlists=allowlists, baseline=baseline, providers=raw.get("providers"),
        settings=raw.get("settings", {}), tools=raw.get("tools", {}),
        decisions_path=raw.get("decisions", "docs/decisions.md"),
    )
    arch.rules = build_rules(arch)
    return arch


def build_rules(arch: Architecture) -> dict[str, Rule]:
    rules: dict[str, Rule] = {}
    for module in arch.modules:
        allowed = (f"may depend only on {', '.join(module.may_depend_on)}" if module.may_depend_on
                   else "may depend on no other module")
        rules[f"boundary:{module.name}"] = Rule(
            f"boundary:{module.name}", module.decision,
            f"[boundary:{module.name}] {module.name} {allowed} ({module.decision}). "
            f"Instead: {module.instead}")
        if module.public:
            rules[f"public:{module.name}"] = Rule(
                f"public:{module.name}", module.decision,
                f"[public:{module.name}] import {module.name} only through {', '.join(module.public)} "
                f"({module.decision}). Instead: {module.instead}")
    for banned in arch.banned:
        rules[banned.id] = Rule(banned.id, banned.decision,
                                f"[{banned.id}] {banned.message} ({banned.decision}). Instead: {banned.instead}")
    for allowlist in arch.allowlists:
        rules[allowlist.id] = Rule(
            allowlist.id, allowlist.decision,
            f"[{allowlist.id}] {allowlist.language} code may use only these external dependencies: "
            f"{', '.join(allowlist.allow) or 'none'} ({allowlist.decision}). Instead: {allowlist.instead}")
    if arch.providers:
        decision = arch.providers.get("decision", "")
        rules["providers"] = Rule(
            "providers", decision,
            f"[providers] {', '.join(arch.providers.get('concerns', []))} enter only through "
            f"{arch.providers.get('module', '')} ({decision}). Instead: {arch.providers.get('instead', '')}")
    return rules


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def read_decisions(path: Path) -> dict[str, list[str]]:
    """Each D- slug in the ledger and the ids on its 'enforced by:' line."""
    decisions: dict[str, list[str]] = {}
    current = None
    if not path.is_file():
        return decisions
    for line in path.read_text(encoding="utf-8").splitlines():
        header = SLUG.match(line)
        if header:
            current = header.group(1)
            decisions.setdefault(current, [])
            continue
        if current and not line.startswith((" ", "\t")):
            current = None
        enforced = ENFORCED.match(line) if current else None
        if enforced:
            decisions[current].extend(part.strip() for part in enforced.group(1).split(",") if part.strip())
    return decisions


def validate(arch: Architecture) -> list[str]:
    problems: list[str] = []
    names = [m.name for m in arch.modules]
    for duplicate in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"module {duplicate} is declared twice")
    for module in arch.modules:
        where = f"module {module.name}"
        if not RULE_ID.match(module.name):
            problems.append(f"{where}: names are lowercase kebab-case")
        if module.layer not in arch.layers:
            problems.append(f"{where}: layer '{module.layer}' is not in 'layers'")
        if not (arch.root / module.path).is_dir():
            problems.append(f"{where}: path {module.path} is not a directory")
        if module.language == "jvm" and not module.package:
            problems.append(f"{where}: a jvm module needs 'package', the Java/Kotlin package it owns")
        if module.public and module.language in ("go", "rust"):
            problems.append(f"{where}: {module.language} enforces privacy in the code itself "
                            f"({'internal/ directories' if module.language == 'go' else 'pub(crate)'}); drop 'public'")
        for entry in module.public:
            if module.language == "jvm":  # public entries are packages, such as com.example.service.api..
                if not entry.startswith(module.package + "."):
                    problems.append(f"{where}: public package {entry} is not inside {module.package}")
            elif not (arch.root / entry).exists():
                problems.append(f"{where}: public entry {entry} does not exist")
        for target in module.may_depend_on:
            if target == module.name:
                problems.append(f"{where}: lists itself in may_depend_on")
            elif target not in names:
                problems.append(f"{where}: may_depend_on names unknown module '{target}'")
            else:
                other = arch.module(target)
                if other.language != module.language:
                    problems.append(f"{where}: may not depend on {target}, which is {other.language}")
                elif (module.layer in arch.layers and other.layer in arch.layers
                      and arch.layers.index(other.layer) > arch.layers.index(module.layer)):
                    problems.append(f"{where}: depends upward on {target} ({other.layer} sits above "
                                    f"{module.layer}); edges point down the layer order")
    problems.extend(cycles(arch))
    if arch.providers:
        provider = arch.providers.get("module")
        if provider not in names:
            problems.append(f"[providers] module '{provider}' is not a declared module")
        for concern in arch.providers.get("concerns", []):
            if concern not in names:
                problems.append(f"[providers] concern '{concern}' is not a declared module")
            for module in arch.modules:
                if concern in module.may_depend_on and module.name != provider:
                    problems.append(f"module {module.name}: depends on {concern} directly; "
                                    f"cross-cutting concerns enter only through {provider}")
        for key in ("decision", "instead"):
            if not arch.providers.get(key):
                problems.append(f"[providers] needs '{key}'")
    ids = [b.id for b in arch.banned]
    for banned in arch.banned:
        where = f"banned {banned.id}"
        if ids.count(banned.id) > 1:
            problems.append(f"{where}: id is used twice")
        if not RULE_ID.match(banned.id) or ":" in banned.id:
            problems.append(f"{where}: ids are lowercase kebab-case")
        language = AST_GREP_LANGUAGES.get(banned.language)
        if language is None:
            problems.append(f"{where}: unknown ast-grep language '{banned.language}'")
        elif language not in arch.languages:
            problems.append(f"{where}: {banned.language} is not one of the file's languages")
        for name in banned.allowed_in:
            if name not in names:
                problems.append(f"{where}: allowed_in names unknown module '{name}'")
        if len(banned.invalid) < 2 or len(banned.valid) < 2:
            problems.append(f"{where}: needs at least 2 invalid examples (the real mistake) and 2 valid "
                            f"near-misses, so the rule is tested before it is trusted")
        if language and not [m for m in arch.in_language(language) if m.name not in banned.allowed_in]:
            problems.append(f"{where}: is allowed in every {language} module, so it bans nothing")
    for allowlist in arch.allowlists:
        if allowlist.language not in arch.languages:
            problems.append(f"[{allowlist.id}] {allowlist.language} is not one of the file's languages")
        if allowlist.language in ("jvm", "cpp"):
            problems.append(f"[{allowlist.id}] no generated check covers {allowlist.language} dependencies; "
                            f"use the build's dependency verification or lockfile instead")
    for entry in arch.baseline:
        if entry["rule"] not in arch.rules:
            problems.append(f"baseline entry for unknown rule '{entry['rule']}'")
        if not (arch.root / entry["from"]).exists():
            problems.append(f"baseline entry {entry['rule']} {entry['from']}: the file is gone, so delete the entry")
    problems.extend(link_decisions(arch))
    return problems


def cycles(arch: Architecture) -> list[str]:
    graph = {m.name: [t for t in m.may_depend_on if t != m.name] for m in arch.modules}
    seen, stack, found = set(), [], []

    def visit(node: str) -> None:
        if node in stack:
            found.append(" -> ".join(stack[stack.index(node):] + [node]))
            return
        if node in seen or node not in graph:
            return
        seen.add(node)
        stack.append(node)
        for nxt in graph[node]:
            visit(nxt)
        stack.pop()

    for node in graph:
        visit(node)
    return [f"dependency cycle: {cycle}" for cycle in found]


def link_decisions(arch: Architecture) -> list[str]:
    """Every rule cites a recorded decision that lists it back, and nothing lists a rule that is gone."""
    problems: list[str] = []
    ledger = arch.root / arch.decisions_path
    decisions = read_decisions(ledger)
    for rule in arch.rules.values():
        if not rule.decision:
            continue
        if rule.decision not in decisions:
            problems.append(f"[{rule.id}] cites {rule.decision}, which {arch.decisions_path} does not record")
        elif rule.id not in decisions[rule.decision]:
            problems.append(f"{rule.decision} does not list {rule.id} on its 'enforced by:' line")
    for slug, ids in decisions.items():
        for rule_id in ids:
            if rule_id.startswith("test:"):
                if not (arch.root / rule_id[5:]).exists():
                    problems.append(f"{slug} is enforced by {rule_id}, but that test file does not exist")
            elif rule_id not in arch.rules:
                problems.append(f"{slug} is enforced by {rule_id}, which {SOURCE} does not define")
    return problems


# ---------------------------------------------------------------------------
# generate
# ---------------------------------------------------------------------------


def quote(text: str) -> str:
    """A YAML scalar; JSON strings are valid YAML."""
    return json.dumps(text)


def header(comment: str) -> str:
    return f"{comment} Generated by arch.py from {SOURCE}. Do not edit: change the toml and run arch.py generate.\n"


def python_module(arch: Architecture, path: str) -> str:
    root = arch.setting("python", "root", ".")
    relative = os.path.relpath(path, root)
    dotted = relative.replace(os.sep, ".")
    for suffix in (".__init__.py", ".py"):
        if dotted.endswith(suffix):
            return dotted[: -len(suffix)]
    return dotted


def plain(rule: Rule) -> str:
    """The message with its id as 'id - ' instead of '[id]', for tools that render brackets as markup."""
    return f"{rule.id} - {rule.message.removeprefix(f'[{rule.id}] ')}"


def gen_python(arch: Architecture) -> dict[str, str]:
    modules = arch.in_language("python")
    if not modules:
        return {}
    packages = {m.name: python_module(arch, m.path) for m in modules}
    roots = sorted({p.split(".")[0] for p in packages.values()})
    out = [header("#").rstrip(), "[importlinter]", f"root_packages =\n    " + "\n    ".join(roots),
           "include_external_packages = False", ""]
    for module in modules:
        forbidden = [packages[m.name] for m in modules if m.name != module.name and m.name not in module.may_depend_on]
        if not forbidden:
            continue
        rule = arch.rules[f"boundary:{module.name}"]
        out += [f"[importlinter:contract:boundary-{module.name}]", f"name = {plain(rule)}", "type = forbidden",
                f"source_modules =\n    {packages[module.name]}",
                "forbidden_modules =\n    " + "\n    ".join(forbidden), "allow_indirect_imports = True"]
        ignores = [f"{python_module(arch, b['from'])} -> {python_module(arch, b['to'])}"
                   for b in arch.baseline_for(rule.id)]
        if ignores:
            out.append("ignore_imports =\n    " + "\n    ".join(ignores))
        out.append("")
    for module in modules:
        if not module.public:
            continue
        rule = arch.rules[f"public:{module.name}"]
        consumers = [packages[m.name] for m in modules if module.name in m.may_depend_on]
        if not consumers:
            continue
        package = packages[module.name]
        public = [python_module(arch, p) for p in module.public]
        ignores = [f"{c}{tail} -> {p}" for c in consumers for tail in ("", ".**") for p in public if p != package]
        ignores += [f"{python_module(arch, b['from'])} -> {python_module(arch, b['to'])}"
                    for b in arch.baseline_for(rule.id)]
        out += [f"[importlinter:contract:public-{module.name}]", f"name = {plain(rule)}", "type = forbidden",
                "source_modules =\n    " + "\n    ".join(consumers), f"forbidden_modules =\n    {package}.**",
                "allow_indirect_imports = True",
                # a consumer need not use every public entry yet, so an unused allowance is no error
                "unmatched_ignore_imports_alerting = none"]
        if ignores:
            out.append("ignore_imports =\n    " + "\n    ".join(ignores))
        out.append("")
    return {f"{OUT}/importlinter.ini": "\n".join(out)}


def regex_path(path: str) -> str:
    return "^" + re.escape(path.rstrip("/") + "/")


def gen_typescript(arch: Architecture) -> dict[str, str]:
    modules = arch.in_language("typescript")
    if not modules:
        return {}
    forbidden = []
    for module in modules:
        others = [m for m in modules if m.name != module.name and m.name not in module.may_depend_on]
        if others:
            rule = arch.rules[f"boundary:{module.name}"]
            forbidden.append({"name": rule.id, "comment": rule.message, "severity": "error",
                              "from": {"path": regex_path(module.path)},
                              "to": {"path": "(" + "|".join(regex_path(m.path) for m in others) + ")"}})
    for module in modules:
        if module.public:
            rule = arch.rules[f"public:{module.name}"]
            public = "(" + "|".join("^" + re.escape(p) + "$" for p in module.public) + ")"
            forbidden.append({"name": rule.id, "comment": rule.message, "severity": "error",
                              "from": {"pathNot": regex_path(module.path)},
                              "to": {"path": regex_path(module.path), "pathNot": public}})
    for allowlist in arch.allowlists:
        if allowlist.language == "typescript":
            rule = arch.rules[allowlist.id]
            allowed = "|".join(re.escape(name) for name in allowlist.allow) or "(?!)"
            forbidden.append({"name": rule.id, "comment": rule.message, "severity": "error",
                              "from": {"path": "(" + "|".join(regex_path(m.path) for m in modules) + ")"},
                              "to": {"dependencyTypes": ["npm", "npm-dev", "npm-optional", "npm-peer",
                                                         "npm-bundled", "npm-no-pkg", "npm-unknown"],
                                     "pathNot": f"node_modules/(?:@types/)?(?:{allowed})/"}})
    options = {"doNotFollow": {"path": "node_modules"}}
    tsconfig = arch.setting("typescript", "tsconfig")
    if tsconfig:
        options["tsConfig"] = {"fileName": tsconfig}
    config = {"forbidden": forbidden, "options": options}
    known = [{"type": "dependency", "from": b["from"], "to": b["to"],
              "rule": {"severity": "error", "name": b["rule"]}} for b in arch.baseline
             if arch.rules.get(b["rule"]) and b["rule"].split(":")[-1] in {m.name for m in modules} | {"typescript"}]
    return {
        f"{OUT}/dependency-cruiser.cjs": header("//") + "module.exports = " + json.dumps(config, indent=2) + ";\n",
        f"{OUT}/dependency-cruiser-known.json": json.dumps(known, indent=2) + "\n",
    }


def go_module_path(arch: Architecture) -> str:
    configured = arch.setting("go", "module")
    if configured:
        return configured
    gomod = arch.root / "go.mod"
    if gomod.is_file():
        match = re.search(r"^module\s+(\S+)", gomod.read_text(encoding="utf-8"), re.MULTILINE)
        if match:
            return match.group(1)
    raise ArchError("go modules need go.mod or [settings.go] module")


def gen_go(arch: Architecture) -> dict[str, str]:
    modules = arch.in_language("go")
    if not modules:
        return {}
    allowlist = next((a for a in arch.allowlists if a.language == "go"), None)
    lines = [header("#").rstrip(), "version: 3", "workdir: .", "allow:",
             f"  depOnAnyVendor: {'false' if allowlist else 'true'}", "components:"]
    for module in modules:
        lines.append(f"  {module.name}: {{ in: {quote(module.path + '/**')} }}")
    excluded = sorted({b["from"] for b in arch.baseline if b["rule"] in {f"boundary:{m.name}" for m in modules}})
    if excluded:
        lines.append("excludeFiles:")
        lines += [f"  - {quote('^' + re.escape(path) + '$')}" for path in excluded]
    if allowlist:
        lines.append("vendors:")
        lines += [f"  {quote(name)}: {{ in: {quote(name + '/**')} }}" for name in allowlist.allow]
    lines.append("deps:")
    for module in modules:
        if not module.may_depend_on and not allowlist:
            continue  # a component with no entry may import no other component
        lines.append(f"  {module.name}:")
        if module.may_depend_on:
            lines.append("    mayDependOn: [" + ", ".join(module.may_depend_on) + "]")
        if allowlist:
            lines.append("    canUse: [" + ", ".join(quote(n) for n in allowlist.allow) + "]")
    return {f"{OUT}/go-arch-lint.yml": "\n".join(lines) + "\n"}


def common_segments(paths: list[list[str]]) -> list[str]:
    """The leading dot-separated segments every package shares (not characters: com.a.service and com.a.store share com.a)."""
    shared = []
    for parts in zip(*paths):
        if len(set(parts)) != 1:
            break
        shared.append(parts[0])
    return shared


def gen_jvm(arch: Architecture) -> dict[str, str]:
    modules = arch.in_language("jvm")
    if not modules:
        return {}
    test_file = arch.setting("jvm", "test_file", "src/test/java/arch/ArchitectureTest.java")
    test_package = arch.setting("jvm", "test_package", "arch")
    base = arch.setting("jvm", "base_package") or ".".join(common_segments([m.package.split(".") for m in modules]))
    frozen = {b["rule"] for b in arch.baseline}
    rules = []
    for module in modules:
        others = [m for m in modules if m.name != module.name and m.name not in module.may_depend_on]
        if others:
            rule = arch.rules[f"boundary:{module.name}"]
            body = (f'noClasses().that().resideInAPackage({json.dumps(module.package + "..")})\n'
                    f'            .should().dependOnClassesThat().resideInAnyPackage('
                    + ", ".join(json.dumps(m.package + "..") for m in others) + ")\n"
                    f"            .because({json.dumps(rule.message)})")
            rules.append((rule.id, body))
        if module.public:
            rule = arch.rules[f"public:{module.name}"]
            public = ", ".join(json.dumps(p) for p in module.public)
            body = (f'noClasses().that().resideOutsideOfPackage({json.dumps(module.package + "..")})\n'
                    f"            .should().dependOnClassesThat(resideInAPackage({json.dumps(module.package + '..')})"
                    f".and(not(resideInAnyPackage({public}))))\n"
                    f"            .because({json.dumps(rule.message)})")
            rules.append((rule.id, body))
    fields = []
    for rule_id, body in rules:
        name = re.sub(r"[^A-Za-z0-9]", "_", rule_id)
        expression = f"FreezingArchRule.freeze({body})" if rule_id in frozen else body
        fields.append(f"    @ArchTest\n    static final ArchRule {name} =\n        {expression};\n")
    source = (header("//") + f"package {test_package};\n\n"
              "import com.tngtech.archunit.junit.AnalyzeClasses;\nimport com.tngtech.archunit.junit.ArchTest;\n"
              "import com.tngtech.archunit.lang.ArchRule;\nimport com.tngtech.archunit.library.freeze.FreezingArchRule;\n\n"
              "import static com.tngtech.archunit.base.DescribedPredicate.not;\n"
              "import static com.tngtech.archunit.core.domain.JavaClass.Predicates.resideInAPackage;\n"
              "import static com.tngtech.archunit.core.domain.JavaClass.Predicates.resideInAnyPackage;\n"
              "import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;\n\n"
              f"@AnalyzeClasses(packages = {json.dumps(base)})\npublic class ArchitectureTest {{\n"
              + "\n".join(fields) + "}\n")
    return {test_file: source}


def module_globs(modules: list[Module], language: str) -> list[str]:
    extensions = EXTENSIONS[AST_GREP_LANGUAGES[language]]
    return [f"{m.path}/**/*{ext}" for m in modules for ext in extensions]


def gen_ast_grep(arch: Architecture) -> dict[str, str]:
    if not arch.banned:
        return {}
    files = {f"{OUT}/sgconfig.yml": header("#") + "ruleDirs:\n  - ast-grep/rules\ntestConfigs:\n  - testDir: ast-grep/tests\n"}
    for banned in arch.banned:
        language = AST_GREP_LANGUAGES[banned.language]
        targets = [m for m in arch.in_language(language) if m.name not in banned.allowed_in]
        ignores = sorted({b["from"] for b in arch.baseline_for(banned.id)})
        rule = arch.rules[banned.id]
        lines = [header("#").rstrip(), f"id: {banned.id}", f"language: {banned.language}", "severity: error",
                 f"message: {quote(rule.message)}", f"note: {quote('Instead: ' + banned.instead)}",
                 "rule:", f"  pattern: {quote(banned.pattern)}", "files:"]
        lines += [f"  - {quote(glob)}" for glob in module_globs(targets, banned.language)]
        if ignores:
            lines.append("ignores:")
            lines += [f"  - {quote(path)}" for path in ignores]
        files[f"{OUT}/ast-grep/rules/{banned.id}.yml"] = "\n".join(lines) + "\n"
        test = [header("#").rstrip(), f"id: {banned.id}", "valid:"] + [f"  - {quote(v)}" for v in banned.valid]
        test += ["invalid:"] + [f"  - {quote(v)}" for v in banned.invalid]
        files[f"{OUT}/ast-grep/tests/{banned.id}-test.yml"] = "\n".join(test) + "\n"
    return files


def generated(arch: Architecture) -> dict[str, str]:
    files: dict[str, str] = {}
    for generator in (gen_python, gen_typescript, gen_go, gen_jvm, gen_ast_grep):
        files.update(generator(arch))
    files[f"{OUT}/arch.py"] = Path(__file__).read_text(encoding="utf-8")
    source = (arch.root / SOURCE).read_bytes()
    manifest = {"source": SOURCE, "source_sha256": hashlib.sha256(source).hexdigest(),
                "files": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in sorted(files.items())}}
    files[f"{OUT}/manifest.json"] = json.dumps(manifest, indent=2) + "\n"
    return files


def generate(arch: Architecture) -> list[str]:
    files = generated(arch)
    old = arch.root / OUT / "manifest.json"
    if old.is_file():
        for path in json.loads(old.read_text(encoding="utf-8")).get("files", {}):
            if path not in files and (arch.root / path).is_file():
                (arch.root / path).unlink()
    for path, text in files.items():
        target = arch.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return sorted(files)


# ---------------------------------------------------------------------------
# Imports, for the built-in checks, infer, and the red team
# ---------------------------------------------------------------------------


def source_files(root: Path, directory: str, language: str) -> list[Path]:
    base = root / directory
    if not base.is_dir():
        return []
    skip = {"node_modules", "__pycache__", ".venv"}
    # build output sits at the top of a repo-wide scan; inside a module a build/ or vendor/ folder can be source
    top_skip = {"target", "build", "dist", "vendor"} if base.resolve() == root.resolve() else set()
    found = []
    for dirpath, dirnames, filenames in os.walk(base):
        blocked = skip | (top_skip if Path(dirpath) == base else set())
        dirnames[:] = [d for d in dirnames if d not in blocked and not d.startswith(".")]
        found += [Path(dirpath) / f for f in filenames if f.endswith(EXTENSIONS[language])]
    return sorted(found)


def python_imports(path: Path, root: Path, package_root: str) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    relative = path.relative_to(root / package_root) if package_root != "." else path.relative_to(root)
    package = list(relative.parent.parts)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1] if node.level > 1 else package
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            found += [f"{prefix}.{alias.name}" if prefix else alias.name for alias in node.names]
    return found


def owner(arch: Architecture, path: str, language: str) -> Module | None:
    matches = [m for m in arch.in_language(language) if path == m.path or path.startswith(m.path + "/")]
    return max(matches, key=lambda m: len(m.path), default=None)


def cpp_includes(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return re.findall(r'^\s*#\s*include\s*"([^"]+)"', text, re.MULTILINE)


def resolve_cpp(arch: Architecture, source: Path, include: str) -> str | None:
    candidates = [source.parent / include] + [arch.root / r / include for r in arch.setting("cpp", "include_roots", [])]
    for candidate in candidates:
        if candidate.is_file():
            return os.path.relpath(candidate.resolve(), arch.root.resolve())
    return None


def cargo(path: Path) -> dict:
    try:
        return toml_loads(path.read_text(encoding="utf-8"))
    except (OSError, TOMLDecodeError):
        return {}


def crate_dependencies(manifest: dict) -> dict[str, dict]:
    deps: dict[str, dict] = {}
    for table in ("dependencies", "dev-dependencies", "build-dependencies"):
        for name, spec in manifest.get(table, {}).items():
            deps[name] = spec if isinstance(spec, dict) else {"version": spec}
    for target in manifest.get("target", {}).values():
        for table in ("dependencies", "dev-dependencies", "build-dependencies"):
            for name, spec in target.get(table, {}).items():
                deps[name] = spec if isinstance(spec, dict) else {"version": spec}
    return deps


# ---------------------------------------------------------------------------
# check: built-in checks for what no generated tool covers
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    rule: str
    source: str
    target: str

    def matches(self, entry: dict) -> bool:
        return (entry["rule"] == self.rule and entry["from"] == self.source
                and (entry["to"] == self.target or self.target.startswith(entry["to"].rstrip("/") + "/")
                     or entry["to"] == self.target.split(".")[0]))

    def baselined(self, arch: Architecture) -> bool:
        return any(self.matches(b) for b in arch.baseline)


def builtin_violations(arch: Architecture) -> list[Violation]:
    found: list[Violation] = []
    found += python_dependency_violations(arch)
    found += rust_violations(arch)
    found += cpp_violations(arch)
    return found


def python_locals(directory: Path) -> set[str]:
    """Names importable from the Python root: .py files and directories that hold Python."""
    if not directory.is_dir():
        return set()
    found = set()
    for entry in directory.iterdir():
        if entry.is_file() and entry.suffix == ".py":
            found.add(entry.stem)
        elif entry.is_dir() and ((entry / "__init__.py").is_file() or any(entry.glob("*.py"))):
            found.add(entry.name)
    return found


def python_syntax_problems(arch: Architecture) -> list[str]:
    """Files the import checks cannot read; skipping them would hide their imports."""
    problems = []
    for module in arch.in_language("python"):
        for path in source_files(arch.root, module.path, "python"):
            try:
                ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, ValueError) as error:
                problems.append(f"{os.path.relpath(path, arch.root)}: cannot be parsed ({type(error).__name__}), "
                                f"so its imports are unchecked; fix the file")
    return problems


def python_dependency_violations(arch: Architecture) -> list[Violation]:
    allowlist = next((a for a in arch.allowlists if a.language == "python"), None)
    if not allowlist:
        return []
    package_root = arch.setting("python", "root", ".")
    local = python_locals(arch.root / package_root)
    allowed = {name.replace("-", "_").lower() for name in allowlist.allow}
    found = []
    for module in arch.in_language("python"):
        for path in source_files(arch.root, module.path, "python"):
            for name in python_imports(path, arch.root, package_root):
                top = name.split(".")[0]
                if not top or top in sys.stdlib_module_names or top in local or top.lower() in allowed:
                    continue
                found.append(Violation(allowlist.id, os.path.relpath(path, arch.root), top))
    return found


def rust_violations(arch: Architecture) -> list[Violation]:
    modules = arch.in_language("rust")
    if not modules:
        return []
    crates = {}
    for module in modules:
        manifest = cargo(arch.root / module.path / "Cargo.toml")
        crates[manifest.get("package", {}).get("name", module.name)] = module
    allowlist = next((a for a in arch.allowlists if a.language == "rust"), None)
    found = []
    for module in modules:
        manifest_path = f"{module.path}/Cargo.toml"
        for name, spec in crate_dependencies(cargo(arch.root / manifest_path)).items():
            crate = spec.get("package", name)
            if crate in crates or "path" in spec:
                target = crates.get(crate)
                if target is None and "path" in spec:
                    target = owner(arch, os.path.normpath(os.path.join(module.path, spec["path"])), "rust")
                if target and target.name != module.name and target.name not in module.may_depend_on:
                    found.append(Violation(f"boundary:{module.name}", manifest_path, target.name))
            elif allowlist and crate not in allowlist.allow:
                found.append(Violation(allowlist.id, manifest_path, crate))
    return found


def cpp_violations(arch: Architecture) -> list[Violation]:
    found = []
    for module in arch.in_language("cpp"):
        for path in source_files(arch.root, module.path, "cpp"):
            source = os.path.relpath(path, arch.root)
            for include in cpp_includes(path):
                target_path = resolve_cpp(arch, path, include)
                target = owner(arch, target_path, "cpp") if target_path else None
                if not target or target.name == module.name:
                    continue
                if target.name not in module.may_depend_on:
                    found.append(Violation(f"boundary:{module.name}", source, target_path))
                elif target.public and not any(target_path == p or target_path.startswith(p.rstrip("/") + "/")
                                               for p in target.public):
                    found.append(Violation(f"public:{target.name}", source, target_path))
    return found


def drift(arch: Architecture) -> list[str]:
    problems = []
    expected = generated(arch)
    manifest_path = arch.root / OUT / "manifest.json"
    if not manifest_path.is_file():
        return [f"{OUT}/manifest.json is missing; run arch.py generate"]
    recorded = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})
    for path, text in expected.items():
        target = arch.root / path
        if not target.is_file():
            problems.append(f"{path} is missing; run arch.py generate")
        elif target.read_text(encoding="utf-8") != text and path == f"{OUT}/arch.py":
            problems.append(f"{path} differs from the arch.py running this check; run arch.py generate "
                            f"with the copy you mean to keep")
        elif target.read_text(encoding="utf-8") != text and path == f"{OUT}/manifest.json":
            continue  # its hashes cover the other files, each reported on its own
        elif target.read_text(encoding="utf-8") != text:
            problems.append(f"{path} differs from what {SOURCE} generates. Generated files are never edited: "
                            f"change {SOURCE} and run arch.py generate")
    for path in recorded:
        if path not in expected and (arch.root / path).exists():
            problems.append(f"{path} is no longer generated; run arch.py generate to remove it")
    return problems


MISSING_REF = object()


def git_show(root: Path, ref: str, path: str) -> str | None | object:
    """The file at the ref; None when the ref resolves but lacks the file; MISSING_REF when the ref is unusable."""
    probe = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root,
                           capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        return MISSING_REF
    result = subprocess.run(["git", "show", f"{ref}:{path}"], cwd=root, capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else None


def loosenings(old: dict, new: dict) -> list[str]:
    """What the new file allows that the old one did not; tightening is always free."""
    found = []
    old_modules = {m["name"]: m for m in old.get("modules", [])}
    new_modules = {m["name"]: m for m in new.get("modules", [])}
    for name, module in old_modules.items():
        if name not in new_modules:
            found.append(f"module {name} and its boundary rule were removed")
            continue
        current = new_modules[name]
        for edge in sorted(set(current.get("may_depend_on", [])) - set(module.get("may_depend_on", []))):
            found.append(f"module {name} may now depend on {edge}")
        if module.get("public") and set(current.get("public", [])) - set(module["public"]):
            found.append(f"module {name} exposes more public entries")
        if module.get("public") and not current.get("public"):
            found.append(f"module {name} no longer restricts imports to its public entries")
    old_banned = {b["id"]: b for b in old.get("banned", [])}
    new_banned = {b["id"]: b for b in new.get("banned", [])}
    for rule_id, banned in old_banned.items():
        if rule_id not in new_banned:
            found.append(f"banned API rule {rule_id} was removed")
        elif set(new_banned[rule_id].get("allowed_in", [])) - set(banned.get("allowed_in", [])):
            found.append(f"banned API rule {rule_id} is allowed in more modules")
        elif new_banned[rule_id].get("pattern") != banned.get("pattern"):
            found.append(f"banned API rule {rule_id} changed its pattern")
    old_allow = {a["language"]: set(a.get("allow", [])) for a in old.get("dependencies", [])}
    new_allow = {a["language"]: set(a.get("allow", [])) for a in new.get("dependencies", [])}
    for language, allowed in old_allow.items():
        if language not in new_allow:
            found.append(f"the {language} dependency allowlist was removed")
        elif new_allow[language] - allowed:
            found.append(f"the {language} dependency allowlist adds {', '.join(sorted(new_allow[language] - allowed))}")
    if old.get("layers", []) != new.get("layers", []):
        old_layers, new_layers = old.get("layers", []), new.get("layers", [])
        for layer in old_layers:
            if layer not in new_layers:
                found.append(f"layer {layer} was removed")
        if [x for x in old_layers if x in new_layers] != [x for x in new_layers if x in old_layers]:
            found.append("the layer order changed")
    if old.get("decisions") != new.get("decisions"):
        found.append("the decisions ledger path changed")
    for table in ("tools", "settings"):
        before, after = old.get(table, {}), new.get(table, {})
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                found.append(f"[{table}] {key} changed; a different command or setting can disable enforcement")
    old_concerns = set((old.get("providers") or {}).get("concerns", []))
    if old_concerns - set((new.get("providers") or {}).get("concerns", [])):
        found.append("cross-cutting concerns left the providers rule")
    key = lambda b: (b["rule"], b["from"], b["to"])
    added = {key(b) for b in new.get("baseline", [])} - {key(b) for b in old.get("baseline", [])}
    for rule, source, target in sorted(added):
        found.append(f"baseline grew: {rule} {source} -> {target}; the baseline only shrinks")
    return found


def check(arch: Architecture, base: str | None, allow_loosening: bool) -> list[str]:
    problems = validate(arch)
    if not problems:  # generated() needs a valid file (for one, a known ast-grep language)
        problems += drift(arch)
    if base:
        old = git_show(arch.root, base, SOURCE)
        if old is MISSING_REF:
            problems.append(f"cannot read {SOURCE} at {base}; fetch the ref (a shallow clone lacks it) or drop --base")
        elif old is not None:  # None: the file is new at the ref, so there was no policy to loosen
            for loosening in loosenings(toml_loads(old), arch.raw):
                if not allow_loosening:
                    problems.append(f"loosening needs a human-reviewed change: {loosening}")
    problems += python_syntax_problems(arch)
    violations = builtin_violations(arch)
    for violation in violations:
        if not violation.baselined(arch):
            problems.append(f"{violation.source} -> {violation.target}: {arch.rules[violation.rule].message}")
    for entry in arch.baseline:
        if entry["rule"].startswith(("dependencies:python", "dependencies:rust")) or is_builtin(arch, entry["rule"]):
            if not any(v.matches(entry) for v in violations):
                problems.append(f"baseline entry {entry['rule']} {entry['from']} -> {entry['to']} no longer matches "
                                f"a violation; delete it so the ratchet tightens")
    return problems


def is_builtin(arch: Architecture, rule_id: str) -> bool:
    kind, _, name = rule_id.partition(":")
    if kind not in ("boundary", "public") or not name:
        return False
    module = next((m for m in arch.modules if m.name == name), None)
    return bool(module and module.language in ("rust", "cpp"))


# ---------------------------------------------------------------------------
# run: the generated tools
# ---------------------------------------------------------------------------


def tool_commands(arch: Architecture) -> list[tuple[str, str]]:
    commands = []
    for language in ("python", "typescript", "go", "jvm"):
        modules = arch.in_language(language)
        if not modules:
            continue
        command = arch.tools.get(language, DEFAULT_TOOLS[language])
        commands.append((language, command.replace("{paths}", " ".join(shlex.quote(m.path) for m in modules))))
    if arch.banned:
        commands.append(("ast-grep", arch.tools.get("ast-grep", DEFAULT_TOOLS["ast-grep"])))
    return commands


def run_command(arch: Architecture, command: str) -> tuple[int, str]:
    env = dict(os.environ)
    python_root = str(arch.root / arch.setting("python", "root", "."))
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [python_root, env.get("PYTHONPATH")]))
    try:
        result = subprocess.run(shlex.split(command), cwd=arch.root, env=env, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return 127, f"{shlex.split(command)[0]} is not installed"
    return result.returncode, re.sub(r"\x1b\[[0-9;]*m", "", result.stdout + result.stderr)


def failing_part(output: str) -> str:
    """The part of a tool's output about failures; import-linter also lists every kept contract."""
    _, marker, broken = output.partition("Broken contracts")
    return broken if marker else output


def remediation(arch: Architecture, output: str) -> list[str]:
    """The message of each rule a failing tool names; go-arch-lint names components, not rules."""
    output = failing_part(output)
    named = [rule for rule in arch.rules.values()
             if f"[{rule.id}]" in output or re.search(rf"(?<![\w:-]){re.escape(rule.id)}(?::| - )", output)
             or (rule.id.startswith("boundary:") and re.search(rf"Component {re.escape(rule.id[9:])}\b", output))]
    return [rule.message for rule in named]


def run(arch: Architecture, base: str | None, allow_loosening: bool) -> int:
    problems = check(arch, base, allow_loosening)
    for problem in problems:
        print(problem)
    failed = bool(problems)
    if not problems:
        print("checks: clean (decisions linked, generated files current, built-in checks)")
    commands = tool_commands(arch)
    if arch.banned:
        commands.append(("ast-grep fixtures", arch.tools.get("ast-grep-test", DEFAULT_TOOLS["ast-grep-test"])))
    for name, command in commands:
        code, output = run_command(arch, command)
        if code == 0:
            print(f"{name}: clean")
            continue
        failed = True
        print(f"{name}: failed ({command})\n{output.rstrip()}")
        fixes = remediation(arch, output)
        if fixes:
            print("Remediation:\n" + "\n".join(f"  {fix}" for fix in fixes))
    return 1 if failed else 0


# ---------------------------------------------------------------------------
# redteam
# ---------------------------------------------------------------------------


@dataclass
class Plant:
    rule: str
    language: str | None  # the tool that must fire; None means the built-in checks
    files: dict[str, str]  # path -> content to write (new files)
    edits: dict[str, str] = field(default_factory=dict)  # path -> full new content (restored after)


def first_file(arch: Architecture, module: Module, exclude: list[str] = ()) -> str | None:
    for path in source_files(arch.root, module.path, module.language):
        relative = os.path.relpath(path, arch.root)
        if PROBE in relative or any(relative == e or relative.startswith(e.rstrip("/") + "/") for e in exclude):
            continue
        if module.language == "cpp" and not relative.endswith((".h", ".hh", ".hpp")):
            continue
        return relative
    return None


def importer(arch: Architecture, source: Module, target_file: str) -> Plant | None:
    """A new file in `source` that imports `target_file`."""
    language = source.language
    probe_dir = source.path
    if language == "python":
        return Plant("", "python", {f"{probe_dir}/{PROBE}.py": f"import {python_module(arch, target_file)}  # noqa\n"})
    if language == "typescript":
        relative = os.path.relpath(os.path.join(arch.root, target_file), os.path.join(arch.root, probe_dir))
        relative = relative if relative.startswith(".") else "./" + relative
        stem = re.sub(r"\.(ts|tsx|js|jsx|mjs|cjs)$", "", relative)
        return Plant("", "typescript", {f"{probe_dir}/{PROBE}.ts": f'import * as probe from "{stem}";\nexport const p = probe;\n'})
    if language == "go":
        existing = next(iter(source_files(arch.root, source.path, "go")), None)
        package = "main"
        if existing:
            match = re.search(r"^package\s+(\w+)", existing.read_text(encoding="utf-8"), re.MULTILINE)
            package = match.group(1) if match else package
        target_dir = os.path.dirname(target_file)
        return Plant("", "go", {f"{probe_dir}/{PROBE}.go": f'package {package}\n\nimport _ "{go_module_path(arch)}/{target_dir}"\n'})
    if language == "jvm":
        target_class = Path(target_file).stem
        target_module = owner(arch, target_file, "jvm")
        package_dir = os.path.relpath(os.path.dirname(target_file), target_module.path).replace(os.sep, ".")
        package = target_module.package + ("" if package_dir == "." else "." + package_dir)
        return Plant("", "jvm", {f"{probe_dir}/ArchRedteamProbe.java":
                                 f"package {source.package};\n\nclass ArchRedteamProbe {{ {package}.{target_class} probe; }}\n"})
    if language == "cpp":
        relative = os.path.relpath(os.path.join(arch.root, target_file), os.path.join(arch.root, probe_dir))
        return Plant("", None, {f"{probe_dir}/{PROBE}.cpp": f'#include "{relative}"\n'})
    return None


def rust_dependency(arch: Architecture, module: Module, line: str) -> Plant:
    manifest = f"{module.path}/Cargo.toml"
    text = (arch.root / manifest).read_text(encoding="utf-8")
    match = re.search(r"^\[dependencies\]\s*$", text, re.MULTILINE)
    edited = (text[: match.end()] + "\n" + line + text[match.end():]) if match else text.rstrip() + f"\n\n[dependencies]\n{line}\n"
    return Plant("", None, {}, {manifest: edited})


def plants(arch: Architecture) -> tuple[list[Plant], list[str]]:
    planted, skipped = [], []
    for module in arch.modules:
        rule_id = f"boundary:{module.name}"
        concerns = set((arch.providers or {}).get("concerns", []))
        candidates = [m for m in arch.in_language(module.language) if m.name != module.name
                      and m.name not in module.may_depend_on]
        candidates.sort(key=lambda m: m.name not in concerns)
        if not candidates:
            skipped.append(f"{rule_id}: {module.name} may depend on every other {module.language} module")
            continue
        target = candidates[0]
        if module.language == "rust":
            crate = cargo(arch.root / target.path / "Cargo.toml").get("package", {}).get("name", target.name)
            relative = os.path.relpath(arch.root / target.path, arch.root / module.path)
            plant = rust_dependency(arch, module, f'{crate} = {{ path = "{relative}" }}')
        else:
            target_file = (target.public[0] if target.public and (arch.root / target.public[0]).is_file()
                           else first_file(arch, target))
            plant = importer(arch, module, target_file) if target_file else None
        if plant is None:
            kind = "header" if module.language == "cpp" else "source file"
            skipped.append(f"{rule_id}: no {kind} in {target.name} to import")
            continue
        plant.rule = rule_id
        planted.append(plant)
    for module in arch.modules:
        if not module.public:
            continue
        rule_id = f"public:{module.name}"
        consumer = next((m for m in arch.modules if module.name in m.may_depend_on), None)
        private = first_file(arch, module, exclude=module.public)
        if not consumer or not private:
            skipped.append(f"{rule_id}: {'no module may depend on it' if not consumer else 'it has no private file'}")
            continue
        plant = importer(arch, consumer, private)
        if plant:
            plant.rule = rule_id
            planted.append(plant)
    for banned in arch.banned:
        language = AST_GREP_LANGUAGES[banned.language]
        module = next((m for m in arch.in_language(language) if m.name not in banned.allowed_in), None)
        if module is None or not banned.invalid:
            continue
        path = f"{module.path}/{PROBE}_{banned.id.replace('-', '_')}{PROBE_EXTENSION[banned.language]}"
        planted.append(Plant(banned.id, "ast-grep", {path: banned.invalid[0] + "\n"}))
    for allowlist in arch.allowlists:
        module = next(iter(arch.in_language(allowlist.language)), None)
        if module is None:
            continue
        if allowlist.language == "python":
            planted.append(Plant(allowlist.id, None, {f"{module.path}/{PROBE}.py": "import arch_redteam_unlisted  # noqa\n"}))
        elif allowlist.language == "rust":
            plant = rust_dependency(arch, module, 'arch-redteam-unlisted = "1"')
            plant.rule = allowlist.id
            planted.append(plant)
        elif allowlist.language == "typescript":
            planted.append(Plant(allowlist.id, "typescript", {
                f"{module.path}/{PROBE}.ts": 'import * as probe from "arch-redteam-unlisted";\nexport const p = probe;\n',
                f"node_modules/arch-redteam-unlisted/package.json": '{"name": "arch-redteam-unlisted", "version": "1.0.0", "main": "index.js"}\n',
                f"node_modules/arch-redteam-unlisted/index.js": "module.exports = {};\n"}))
        elif allowlist.language == "go":
            skipped.append(f"{allowlist.id}: planting an unlisted Go module needs it in go.mod; covered by go-arch-lint's vendor check")
    return planted, skipped


def fired(arch: Architecture, plant: Plant) -> tuple[bool, str]:
    if plant.language is None:
        hits = [v for v in builtin_violations(arch) if v.rule == plant.rule and not v.baselined(arch)]
        return bool(hits), f"built-in check {'reported' if hits else 'did not report'} {plant.rule}"
    if plant.language == "ast-grep":
        command = arch.tools.get("ast-grep", DEFAULT_TOOLS["ast-grep"])
    else:
        command = dict(tool_commands(arch)).get(plant.language)
    code, output = run_command(arch, command)
    if code == 127:
        return False, output.strip()
    marker = plant.rule
    if plant.language == "go":  # go-arch-lint names the component, not the rule
        marker = f"Component {plant.rule.partition(':')[2]} shouldn't depend"
    named = names_marker(marker, failing_part(output))
    return code != 0 and named, f"{command} exited {code}" + ("" if named else f" without naming {plant.rule}")


def names_marker(marker: str, output: str) -> bool:
    """Whether the output names the rule itself, not a longer id that starts with it (boundary:api-v2)."""
    return re.search(r"(?<![\w-])" + re.escape(marker) + r"(?![\w-])", output) is not None


def redteam(arch: Architecture) -> int:
    planted, skipped = plants(arch)
    covered = {p.rule for p in planted}
    failures = 0
    for plant in planted:
        created, originals = [], {}
        try:
            for path, text in plant.files.items():
                target = arch.root / path
                if target.exists():
                    continue
                created += [p for p in [*reversed(target.relative_to(arch.root).parents)][1:] if not (arch.root / p).exists()]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                created.append(Path(path))
            for path, text in plant.edits.items():
                originals[path] = (arch.root / path).read_text(encoding="utf-8")
                (arch.root / path).write_text(text, encoding="utf-8")
            ok, detail = fired(arch, plant)
        finally:
            for path, text in originals.items():
                (arch.root / path).write_text(text, encoding="utf-8")
            for path in reversed(created):
                target = arch.root / path
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
        print(f"{'fires' if ok else 'GREEN'}  {plant.rule}: {detail}")
        failures += not ok
    for reason in skipped:
        print(f"skip   {reason}")
    for rule_id in sorted(set(arch.rules) - covered - {r.split(':')[0] for r in skipped} - {"providers"}):
        if not any(s.startswith(rule_id + ":") for s in skipped):
            print(f"skip   {rule_id}: no violation could be planted")
    if failures:
        print(f"\n{failures} rule(s) stayed green on a planted violation; they enforce nothing until fixed")
        return 1
    print(f"\nevery planted violation failed ({len(planted)} rules)")
    return 0


# ---------------------------------------------------------------------------
# infer
# ---------------------------------------------------------------------------


def infer(root: Path, language: str, base: str, depth: int) -> int:
    base_dir = root / base
    if not base_dir.is_dir():
        print(f"no directory {base}", file=sys.stderr)
        return 2
    candidates = sorted(p for p in base_dir.glob("/".join(["*"] * depth)) if p.is_dir() and not p.name.startswith((".", "_")))
    modules = {os.path.relpath(p, root): p.name for p in candidates}
    edges: dict[tuple[str, str], int] = {}
    import_root = base_dir
    while (import_root / "__init__.py").is_file():  # python imports are relative to the top package's parent
        import_root = import_root.parent
    import_root_name = os.path.relpath(import_root, root)
    module_path = go_module_path_raw(root) if language == "go" else None

    def module_of(path: str) -> str | None:
        return next((name for directory, name in modules.items() if path == directory or path.startswith(directory + "/")), None)

    for directory, name in modules.items():
        for path in source_files(root, directory, language):
            targets: list[str] = []
            if language == "python":
                for dotted in python_imports(path, root, import_root_name):
                    targets.append(os.path.normpath(os.path.join(import_root_name, *dotted.split("."))))
            elif language == "typescript":
                text = path.read_text(encoding="utf-8", errors="replace")
                for spec in re.findall(r"""(?:from\s+|import\s*\(?\s*|require\(\s*)['"](\.[^'"]+)['"]""", text):
                    targets.append(os.path.relpath(os.path.normpath(path.parent / spec), root))
            elif language == "go":
                text = path.read_text(encoding="utf-8", errors="replace")
                for spec in re.findall(r'^\s*(?:import\s+)?(?:\w+\s+)?"([^"]+)"', text, re.MULTILINE):
                    if module_path and spec.startswith(module_path + "/"):
                        targets.append(spec[len(module_path) + 1:])
            elif language == "jvm":
                text = path.read_text(encoding="utf-8", errors="replace")
                for spec in re.findall(r"^import\s+(?:static\s+)?([\w.]+)", text, re.MULTILINE):
                    targets.append(os.path.join(base, *spec.split(".")[:-1]))
            elif language == "cpp":
                for include in cpp_includes(path):
                    candidate = path.parent / include
                    if candidate.is_file():
                        targets.append(os.path.relpath(candidate.resolve(), root.resolve()))
            for target in targets:
                other = module_of(target)
                if other and other != name:
                    edges[(name, other)] = edges.get((name, other), 0) + 1
    print(f"# {len(modules)} candidate modules under {base}, {len(edges)} observed edges")
    for (source, target), count in sorted(edges.items(), key=lambda e: -e[1]):
        print(f"#   {source} -> {target}: {count} import(s)")
    reverse = {(t, s) for s, t in edges}
    for source, target in sorted(e for e in edges if e in reverse and e[0] < e[1]):
        print(f"#   cycle: {source} <-> {target} - pick a direction; the rarer side is a baseline candidate")
    print("\n# Draft: set each layer, cut edges that should not exist, and argue each choice as a D- decision.")
    for directory, name in modules.items():
        deps = sorted({t for s, t in edges if s == name})
        print(f'\n[[modules]]\nname = "{name}"\nlayer = "?"\nlanguage = "{language}"\npath = "{directory}"\n'
              f"may_depend_on = {json.dumps(deps)}\ndecision = \"D-?\"\ninstead = \"?\"")
    return 0


def go_module_path_raw(root: Path) -> str | None:
    gomod = root / "go.mod"
    if gomod.is_file():
        match = re.search(r"^module\s+(\S+)", gomod.read_text(encoding="utf-8"), re.MULTILINE)
        return match.group(1) if match else None
    return None


# ---------------------------------------------------------------------------


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="arch.py", description=__doc__.split("\n")[0])
    parser.add_argument("command", choices=("validate", "generate", "check", "run", "redteam", "infer"))
    parser.add_argument("--root", default=".", help="repository root (default: .)")
    parser.add_argument("--base", help="git ref to compare the file against for loosening")
    parser.add_argument("--allow-loosening", action="store_true", help="accept a wider rule set (human-reviewed changes only)")
    parser.add_argument("--language", choices=LANGUAGES, help="infer: the language to read")
    parser.add_argument("--dir", default=".", help="infer: the directory whose subdirectories are candidate modules")
    parser.add_argument("--depth", type=positive_int, default=1, help="infer: how many levels below --dir a module sits")
    args = parser.parse_args(argv[1:])
    root = Path(args.root).resolve()
    if args.command == "infer":
        if not args.language:
            parser.error("infer needs --language")
        return infer(root, args.language, args.dir, args.depth)
    try:
        arch = load(root)
        if args.command == "validate":
            problems = validate(arch)
        elif args.command == "generate":
            problems = validate(arch)
            if not problems:
                for path in generate(arch):
                    print(f"wrote {path}")
        elif args.command == "check":
            problems = check(arch, args.base, args.allow_loosening)
        elif args.command == "run":
            return run(arch, args.base, args.allow_loosening)
        else:
            problems = validate(arch)
            problems += [] if problems else drift(arch)
            if not problems:
                return redteam(arch)
    except ArchError as error:
        print(f"{SOURCE}: {error}")
        return 1
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} problem(s)")
        return 1
    print(f"{SOURCE}: {args.command} clean - {len(arch.rules)} rules")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
