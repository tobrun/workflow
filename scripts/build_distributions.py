#!/usr/bin/env python3
"""Build every distribution of the dev skills from their source under dev/.

Usage: python3 scripts/build_distributions.py [--check]

- skills/{name}/      the portable Agent Skills tree (agentskills.io): spec-only frontmatter,
                      and each skill self-contained - the shared references and scripts it
                      uses are copied in and every link rewritten to match - plus the Codex
                      sidecar agents/openai.yaml. npx skills, gh skill, Pi, opencode, a manual
                      copy into .agents/skills/, and the Codex plugin all install from here.
- .codex-plugin/      the Codex plugin manifest at the repository root, pointing at skills/.

One portable copy, not one per harness: installers that scan the repository would
otherwise find each skill twice.

dev/ stays the Claude Code plugin, its skills under dev/source/ (named so installers that
scan for skills/ folders find only the portable copy), where disable-model-invocation
keeps every skill manual.
--check fails when a generated tree differs from a fresh build.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = "dev"
REPOSITORY = "https://github.com/tobrun/workflow"
GENERATOR = "scripts/build_distributions.py"
# Markers of trees an earlier generator wrote, so a retired distribution is still recognized.
GENERATED_MARKERS = (GENERATOR, "scripts/build_codex_plugin.py")
SHARED = ("references", "scripts")
# Not "skills": installers scan a repository for skills/ folders, and must find only the portable copy.
SOURCE_SKILLS = "source"
JUNK = shutil.ignore_patterns(".DS_Store", "__pycache__")
LINK = re.compile(r"\]\(([^)\s#]+)(#[^)\s]*)?\)")
SKILL_ROOT = re.compile(r"\{([a-z]+)-skill-root\}(/[^\s`'\")\]]*)?")
NEEDS_SIBLINGS = "Reads the scope, build, and ship skills installed beside it; install the whole set."


@dataclass(frozen=True)
class SkillConfig:
    display_name: str
    short_description: str
    default_prompt: str
    compatibility: str


@dataclass(frozen=True)
class PluginConfig:
    name: str
    display_name: str
    short_description: str
    capabilities: tuple[str, ...]
    default_prompts: tuple[str, ...]
    skills: dict[str, SkillConfig]

    @property
    def source(self) -> Path:
        return ROOT / self.name

    @property
    def destination(self) -> Path:
        return ROOT / "plugins" / self.name


GIT_PYTHON = "Needs git and Python 3.10+."
PLUGINS = {
    "dev": PluginConfig(
        name="dev",
        display_name="Dev Workflow",
        short_description="Scope, build, and ship tested changes.",
        capabilities=("Interactive", "Write"),
        default_prompts=(
            "Scope this change with argued decisions.",
            "Build the current spec with tests at every layer.",
            "Verify this change, review it, and open a pull request with proof.",
        ),
        skills={
            "commit": SkillConfig("Commit", "Create granular commits with structured messages",
                                  "Use $dev:commit to group the pending changes into granular, well-explained commits.",
                                  "Needs git."),
            "build": SkillConfig("Build", "Execute a spec with tests through e2e",
                                 "Use $dev:build to execute the current spec with its tests and verify it end to end.",
                                 f"{GIT_PYTHON} Runs parallel subagents when the host has them."),
            "scope": SkillConfig("Scope", "Spec a change by arguing its decisions",
                                 "Use $dev:scope to spec this change with argued decisions and a change plan.",
                                 GIT_PYTHON),
            "ship": SkillConfig("Ship", "Verify, review, then open a PR with proof",
                                "Use $dev:ship to run every kind of verification, the verified review, and open the pull request with evidence for this change.",
                                f"{GIT_PYTHON} Needs subagents or the Pi runner for the review panel; gh to open the pull request."),
            "bootstrap": SkillConfig("Bootstrap", "Enforce the architecture from one file",
                                     "Use $dev:bootstrap to infer this repository's architecture, write docs/architecture.toml, and wire the generated checks into CI.",
                                     f"{GIT_PYTHON} Writes GitHub Actions workflows; other CI hosts are wired by hand."),
            "yolo": SkillConfig("Yolo", "Scope, build, and ship one task with no questions",
                                "Use $dev:yolo to take this task from spec to an open pull request without asking me anything.",
                                f"{GIT_PYTHON} {NEEDS_SIBLINGS}"),
        },
    ),
}


def json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


# ---------------------------------------------------------------------------
# The portable tree
# ---------------------------------------------------------------------------


def split_frontmatter(text: str, where: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not match:
        raise ValueError(f"{where}: no YAML frontmatter")
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields, text[match.end():]


def portable_frontmatter(fields: dict[str, str], name: str, skill: SkillConfig, version: str) -> str:
    """The six spec fields only; Claude's disable-model-invocation stays in the Claude plugin."""
    if fields.get("disable-model-invocation") != "true":
        raise ValueError(f"{name}: the source SKILL.md must set disable-model-invocation: true")
    return (
        "---\n"
        f"name: {fields['name']}\n"
        f"description: {fields['description']}\n"
        "license: MIT\n"
        f"compatibility: {skill.compatibility}\n"
        "metadata:\n"
        f"  version: {json.dumps(version)}\n"
        f"  source: {json.dumps(REPOSITORY)}\n"
        '  invocation: "explicit - recommend it, never run it unasked"\n'
        "---\n"
    )


class Portable:
    """Builds one skill's self-contained folder under {out}/skills/{name}."""

    def __init__(self, plugin: PluginConfig, out: Path, name: str) -> None:
        self.plugin, self.out, self.name = plugin, out, name
        self.source_skills = plugin.source / SOURCE_SKILLS
        self.root = out / name
        self.origin: dict[Path, Path] = {}  # file in the portable tree -> its source file

    def place(self, source: Path, where: str) -> Path:
        """Where a source file lives in the portable tree, vendoring a shared one into this skill."""
        source = Path(os.path.normpath(source))
        skills = self.source_skills.resolve()
        if source.is_relative_to(skills):
            relative = source.relative_to(skills)
            return self.out / relative
        for shared in SHARED:
            base = (self.plugin.source / shared).resolve()
            if source.is_relative_to(base):
                if not source.exists():
                    raise ValueError(f"{where}: links to {source}, which does not exist")
                target = self.root / shared / source.relative_to(base)
                own = self.source_skills / self.name / shared / source.relative_to(base)
                if own.exists() and own.resolve() != source:
                    raise ValueError(f"{where}: shared {source.name} collides with the skill's own {own}")
                if target not in self.origin:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if source.is_dir():
                        shutil.copytree(source, target, ignore=JUNK, dirs_exist_ok=True)
                    else:
                        shutil.copy2(source, target)
                    self.origin[target] = source
                    if target.suffix == ".md":
                        self.rewrite(target)
                return target
        raise ValueError(f"{where}: links to {source}, outside the plugin's skills and shared folders")

    def rewrite(self, path: Path) -> None:
        source = self.origin[path]
        text = path.read_text(encoding="utf-8")
        where = os.path.relpath(source, ROOT)

        def link(match: re.Match) -> str:
            target, anchor = match.group(1), match.group(2) or ""
            if re.match(r"^[a-z]+:|^/|^\{", target):
                return match.group(0)
            new = self.place(source.parent.resolve() / target, where)
            return f"]({os.path.relpath(new, path.parent)}{anchor})"

        def skill_root(match: re.Match) -> str:
            owner, rest = match.group(1), match.group(2) or ""
            new = self.place((self.source_skills / owner).resolve() / rest.lstrip("/"), where)
            relative = os.path.relpath(new, self.root)
            if relative.startswith(".."):
                sibling = os.path.relpath(new, self.out)
                skill, _, inner = sibling.partition("/")
                return f"{{{skill}-skill-root}}" + (f"/{inner}" if inner else "")
            return f"{{{self.name}-skill-root}}" + ("" if relative == "." else f"/{relative}")

        text = LINK.sub(link, text)
        text = SKILL_ROOT.sub(skill_root, text)
        path.write_text(text, encoding="utf-8")

    def build(self, version: str) -> None:
        source_root = self.source_skills / self.name
        shutil.copytree(source_root, self.root, ignore=JUNK)
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                self.origin[path] = (source_root / path.relative_to(self.root)).resolve()
        for path in sorted(p for p in list(self.origin) if p.suffix == ".md"):
            self.rewrite(path)
        skill_md = self.root / "SKILL.md"
        fields, body = split_frontmatter(skill_md.read_text(encoding="utf-8"), f"{self.name}/SKILL.md")
        skill_md.write_text(portable_frontmatter(fields, self.name, self.plugin.skills[self.name], version) + body,
                            encoding="utf-8")


def build_portable(plugin: PluginConfig, out: Path) -> list[str]:
    version = json.loads((plugin.source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    names = sorted(p.name for p in (plugin.source / SOURCE_SKILLS).iterdir() if (p / "SKILL.md").is_file())
    unlisted = [n for n in names if n not in plugin.skills]
    orphaned = [n for n in plugin.skills if n not in names]
    if unlisted or orphaned:
        raise ValueError(f"skill configs out of sync with {plugin.name}/{SOURCE_SKILLS}/: missing {unlisted}, stale {orphaned}")
    out.mkdir(parents=True, exist_ok=True)
    for name in names:
        Portable(plugin, out, name).build(version)
    (out / "README.md").write_text(
        f"# Portable skills\n\nGenerated from `{plugin.name}/{SOURCE_SKILLS}/` by `{GENERATOR}`; do not edit.\n"
        "Each folder is a self-contained Agent Skill (agentskills.io). Install the whole set - `yolo` reads `scope`,\n"
        "`build`, and `ship` beside it. See the repository README for every harness.\n",
        encoding="utf-8")
    return names


# ---------------------------------------------------------------------------
# The Codex plugin
# ---------------------------------------------------------------------------


def openai_yaml(skill: SkillConfig) -> str:
    return (
        "interface:\n"
        f"  display_name: {json.dumps(skill.display_name)}\n"
        f"  short_description: {json.dumps(skill.short_description)}\n"
        f"  default_prompt: {json.dumps(skill.default_prompt)}\n"
        "policy:\n"
        "  allow_implicit_invocation: false\n"
    )


def build_codex(plugin: PluginConfig, portable: Path, destination: Path) -> None:
    """The sidecars go into the portable tree; the manifest at the destination points at it."""
    manifest_source = json.loads((plugin.source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    for name, skill in plugin.skills.items():
        agents = portable / name / "agents"
        agents.mkdir(exist_ok=True)
        (agents / "openai.yaml").write_text(openai_yaml(skill), encoding="utf-8")
    description = manifest_source["description"]
    manifest = {
        "name": plugin.name,
        "version": manifest_source["version"],
        "description": description,
        "author": manifest_source["author"],
        "repository": REPOSITORY,
        "skills": "./skills/",
        "interface": {
            "displayName": plugin.display_name,
            "shortDescription": plugin.short_description,
            "longDescription": description,
            "developerName": manifest_source["author"]["name"],
            "category": "Developer Tools",
            "capabilities": list(plugin.capabilities),
            "defaultPrompt": list(plugin.default_prompts),
        },
    }
    (destination / ".codex-plugin").mkdir(parents=True, exist_ok=True)
    (destination / ".codex-plugin" / "plugin.json").write_text(json_text(manifest), encoding="utf-8")


# ---------------------------------------------------------------------------


def trees() -> list[str]:
    return ["skills", ".codex-plugin"] if PLUGINS else []


def build_all(root: Path) -> None:
    if len(PLUGINS) > 1:
        raise ValueError("one portable tree serves one plugin; a second needs its own subdirectory")
    for plugin in PLUGINS.values():
        build_portable(plugin, root / "skills")
        build_codex(plugin, root / "skills", root)


def snapshot(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in sorted(root.rglob("*"))
            if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts}


def retired_plugins() -> list[Path]:
    distributions = ROOT / "plugins"
    if not distributions.is_dir():
        return []
    found = []
    for path in sorted(distributions.iterdir()):
        readme = path / "README.md"
        # Every generated Codex plugin under plugins/ is retired: Codex installs from skills/ now.
        if (path.is_dir() and not path.is_symlink()
                and (path / ".codex-plugin" / "plugin.json").is_file() and readme.is_file()
                and any(f"Generated from `{path.name}/` by `{marker}`." in readme.read_text(encoding="utf-8")
                        for marker in GENERATED_MARKERS)):
            found.append(path)
    return found


def check() -> int:
    status = 0
    for path in retired_plugins():
        print(f"retired: plugins/{path.name}; run {GENERATOR}")
        status = 1
    with tempfile.TemporaryDirectory(prefix="distributions-") as temp:
        expected = Path(temp)
        build_all(expected)
        for tree in trees():
            want = snapshot(expected / tree)
            have = snapshot(ROOT / tree) if (ROOT / tree).is_dir() else {}
            for label, paths in (("missing", want.keys() - have.keys()), ("extra", have.keys() - want.keys()),
                                 ("changed", {p for p in want.keys() & have.keys() if want[p] != have[p]})):
                for path in sorted(paths):
                    print(f"{label}: {tree}/{path}")
                    status = 1
    if status:
        print(f"Run: python3 {GENERATOR}")
    else:
        print("Distributions are up to date.")
    return status


def write() -> None:
    with tempfile.TemporaryDirectory(prefix=".distributions-", dir=ROOT) as temp:
        built = Path(temp)
        build_all(built)  # build everything before replacing anything
        for tree in trees():
            destination = ROOT / tree
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                shutil.rmtree(destination)
            (built / tree).rename(destination)
            print(f"Built {tree}/")
    for path in retired_plugins():
        shutil.rmtree(path)
        print(f"Removed retired Codex plugin: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true", help="fail when a generated tree differs from a fresh build")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.check:
        return check()
    write()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
