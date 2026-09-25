#!/usr/bin/env python3
"""Build the Codex plugins from their Claude-compatible source trees."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class PluginConfig:
    name: str
    display_name: str
    short_description: str
    capabilities: tuple[str, ...]
    default_prompts: tuple[str, ...]
    # skill name -> (display name, short description, default prompt)
    skill_ui: dict[str, tuple[str, str, str]]
    copied_dirs: tuple[str, ...] = ("skills", "references", "scripts")

    @property
    def source(self) -> Path:
        return ROOT / self.name

    @property
    def destination(self) -> Path:
        return ROOT / "plugins" / self.name


PLUGINS = {
    "dev": PluginConfig(
        name="dev",
        display_name="Dev Workflow",
        short_description="Scope, build, and ship tested changes.",
        capabilities=("Interactive", "Write"),
        default_prompts=(
            "Scope this change with argued decisions.",
            "Build the current spec test-first.",
            "Ship this change with the gauntlet and a verified review.",
        ),
        skill_ui={
            "commit": (
                "Commit",
                "Create granular commits with structured messages",
                "Use $dev:commit to group the pending changes into granular, well-explained commits.",
            ),
            "build": (
                "Build",
                "Execute a spec test-first through e2e",
                "Use $dev:build to execute the current spec test-first and verify it end to end.",
            ),
            "scope": (
                "Scope",
                "Spec a change by arguing its decisions",
                "Use $dev:scope to spec this change with argued decisions and a change plan.",
            ),
            "scope-review": (
                "Scope Review",
                "Review and auto-refine a settled spec",
                "Use $dev:scope-review to review the settled spec with a verified agent panel and refine it in place before building.",
            ),
            "ship": (
                "Ship",
                "Harden, review, then open a PR with proof",
                "Use $dev:ship to run the quality gauntlet, the verified review, and open the pull request with evidence for this change.",
            ),
            "to-pitch": (
                "To Pitch",
                "Turn finished work into a buy-in document",
                "Use $dev:to-pitch to create a buy-in document for the completed change.",
            ),
            "to-quiz": (
                "To Quiz",
                "Create a graded change comprehension quiz",
                "Use $dev:to-quiz to create a comprehension check for the completed change.",
            ),
        },
    ),
    "factory": PluginConfig(
        name="factory",
        display_name="Factory",
        short_description="Drive a declared pipeline of phases unattended.",
        capabilities=("Interactive", "Write"),
        default_prompts=(
            "Take this request through scope, then run it unattended to a pull request.",
        ),
        copied_dirs=("skills", "phases", "references", "scripts"),
        skill_ui={
            "run": (
                "Run",
                "Take a request from scope to a shipped change unattended",
                "Use $factory:run to drive the pipeline declared in .factory/config.yaml, or the built-in scope, scope-review, build, and ship, unattended.",
            ),
        },
    ),
    "bootstrap": PluginConfig(
        name="bootstrap",
        display_name="Bootstrap",
        short_description="Write a repository's AGENTS.md from a probe of its stack.",
        capabilities=("Interactive", "Write"),
        default_prompts=("Write or refresh this repository's AGENTS.md.",),
        copied_dirs=("skills",),
        skill_ui={
            "agents-md": (
                "AGENTS.md",
                "Probe the stack and write a root AGENTS.md",
                "Use $bootstrap:agents-md to probe this repository and write or refresh its root AGENTS.md.",
            ),
        },
    ),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when a generated plugin differs from a fresh build.",
    )
    parser.add_argument(
        "--plugin",
        choices=sorted(PLUGINS),
        help="Build or check only this plugin (default: all).",
    )
    return parser.parse_args()


def json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def codex_skill(contents: str, skill_name: str) -> str:
    marker = "disable-model-invocation: true\n"
    if marker not in contents:
        raise ValueError(f"{skill_name}: missing Claude invocation policy")
    return contents.replace(marker, "", 1)


def openai_yaml(config: PluginConfig, skill_name: str) -> str:
    display_name, short_description, default_prompt = config.skill_ui[skill_name]
    return (
        "interface:\n"
        f"  display_name: {json.dumps(display_name)}\n"
        f"  short_description: {json.dumps(short_description)}\n"
        f"  default_prompt: {json.dumps(default_prompt)}\n"
        "policy:\n"
        "  allow_implicit_invocation: false\n"
    )


def strip_phase_policy(destination: Path) -> None:
    """Phase bodies are read by path, not invoked, so they need no UI entry - only the strip."""
    for skill_md in sorted((destination / "phases").glob("*/SKILL.md")):
        skill_md.write_text(
            codex_skill(skill_md.read_text(encoding="utf-8"), skill_md.parent.name),
            encoding="utf-8",
        )


def build(config: PluginConfig, destination: Path) -> None:
    source = config.source
    claude_manifest = json.loads(
        (source / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    description = claude_manifest["description"]
    junk = shutil.ignore_patterns(".DS_Store", "__pycache__")
    for directory in config.copied_dirs:
        shutil.copytree(source / directory, destination / directory, ignore=junk)

    skill_names = sorted(
        path.name for path in (destination / "skills").iterdir() if path.is_dir()
    )
    unlisted = [name for name in skill_names if name not in config.skill_ui]
    orphaned = [name for name in config.skill_ui if name not in skill_names]
    if unlisted or orphaned:
        raise ValueError(
            f"skill_ui out of sync with {config.name}/skills/: "
            f"missing entries {unlisted}, stale entries {orphaned}"
        )

    for skill_name in skill_names:
        skill_root = destination / "skills" / skill_name
        skill_md = skill_root / "SKILL.md"
        skill_md.write_text(
            codex_skill(skill_md.read_text(encoding="utf-8"), skill_name),
            encoding="utf-8",
        )
        agent_dir = skill_root / "agents"
        agent_dir.mkdir(exist_ok=True)
        (agent_dir / "openai.yaml").write_text(
            openai_yaml(config, skill_name),
            encoding="utf-8",
        )

    strip_phase_policy(destination)

    manifest = {
        "name": config.name,
        "version": claude_manifest["version"],
        "description": description,
        "author": claude_manifest["author"],
        "repository": "https://github.com/tobrun/workflow",
        "skills": "./skills/",
        "interface": {
            "displayName": config.display_name,
            "shortDescription": config.short_description,
            "longDescription": description,
            "developerName": claude_manifest["author"]["name"],
            "category": "Developer Tools",
            "capabilities": list(config.capabilities),
            "defaultPrompt": list(config.default_prompts),
        },
    }
    manifest_dir = destination / ".codex-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json_text(manifest), encoding="utf-8")
    (destination / "README.md").write_text(
        f"# {config.name} for Codex\n\n"
        f"Generated from `{config.name}/` by `scripts/build_codex_plugin.py`. "
        "Do not edit this directory directly.\n",
        encoding="utf-8",
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store" and "__pycache__" not in path.parts
    }


def check(config: PluginConfig) -> int:
    with tempfile.TemporaryDirectory(prefix="codex-plugin-") as temp:
        expected = Path(temp) / config.name
        build(config, expected)
        destination = config.destination
        actual_files = snapshot(destination) if destination.is_dir() else {}
        expected_files = snapshot(expected)

    missing = sorted(expected_files.keys() - actual_files.keys())
    extra = sorted(actual_files.keys() - expected_files.keys())
    changed = sorted(
        path
        for path in expected_files.keys() & actual_files.keys()
        if expected_files[path] != actual_files[path]
    )
    if not (missing or extra or changed):
        print(f"Codex plugin {config.name} is up to date.")
        return 0
    for label, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
        for path in paths:
            print(f"{label}: plugins/{config.name}/{path}")
    print(f"Run: python3 scripts/build_codex_plugin.py --plugin {config.name}")
    return 1


def write(config: PluginConfig) -> None:
    destination = config.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Build in a temporary sibling and swap only after the whole plugin succeeded.
    with tempfile.TemporaryDirectory(
        prefix=f".{config.name}-build-", dir=destination.parent
    ) as temp:
        built = Path(temp) / config.name
        build(config, built)
        previous = Path(temp) / "previous"
        if destination.exists():
            destination.rename(previous)
        built.rename(destination)
    print(f"Built Codex plugin: {destination}")


def main() -> int:
    args = parse_args()
    selected = [PLUGINS[args.plugin]] if args.plugin else list(PLUGINS.values())
    if args.check:
        return max(check(config) for config in selected)
    for config in selected:
        write(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
