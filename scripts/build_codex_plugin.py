#!/usr/bin/env python3
"""Build the Codex dev plugin from the Claude-compatible source tree."""

from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "dev"
DESTINATION = ROOT / "plugins" / "dev"

SKILL_UI = {
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
        "Harden then review a change with verification",
        "Use $dev:ship to run the quality gauntlet and the verified review over this change.",
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
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when plugins/dev differs from a fresh build.",
    )
    return parser.parse_args()


def json_text(payload: object) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=True) + "\n"


def codex_skill(contents: str, skill_name: str) -> str:
    marker = "disable-model-invocation: true\n"
    if marker not in contents:
        raise ValueError(f"{skill_name}: missing Claude invocation policy")
    return contents.replace(marker, "", 1)


def openai_yaml(skill_name: str) -> str:
    display_name, short_description, default_prompt = SKILL_UI[skill_name]
    return (
        "interface:\n"
        f"  display_name: {json.dumps(display_name)}\n"
        f"  short_description: {json.dumps(short_description)}\n"
        f"  default_prompt: {json.dumps(default_prompt)}\n"
        "policy:\n"
        "  allow_implicit_invocation: false\n"
    )


def build(destination: Path) -> None:
    claude_manifest = json.loads(
        (SOURCE / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    description = claude_manifest["description"]
    junk = shutil.ignore_patterns(".DS_Store", "__pycache__")
    shutil.copytree(SOURCE / "skills", destination / "skills", ignore=junk)
    shutil.copytree(SOURCE / "references", destination / "references", ignore=junk)
    shutil.copytree(SOURCE / "scripts", destination / "scripts", ignore=junk)

    skill_names = sorted(
        path.name for path in (destination / "skills").iterdir() if path.is_dir()
    )
    unlisted = [name for name in skill_names if name not in SKILL_UI]
    orphaned = [name for name in SKILL_UI if name not in skill_names]
    if unlisted or orphaned:
        raise ValueError(
            f"SKILL_UI out of sync with dev/skills/: "
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
            openai_yaml(skill_name),
            encoding="utf-8",
        )

    manifest = {
        "name": "dev",
        "version": claude_manifest["version"],
        "description": description,
        "author": claude_manifest["author"],
        "repository": "https://github.com/tobrun/workflow",
        "skills": "./skills/",
        "interface": {
            "displayName": "Dev Workflow",
            "shortDescription": "Scope, build, and ship tested changes.",
            "longDescription": description,
            "developerName": claude_manifest["author"]["name"],
            "category": "Developer Tools",
            "capabilities": ["Interactive", "Write"],
            "defaultPrompt": [
                "Scope this change with argued decisions.",
                "Build the current spec test-first.",
                "Ship this change with the gauntlet and a verified review.",
            ],
        },
    }
    manifest_dir = destination / ".codex-plugin"
    manifest_dir.mkdir(exist_ok=True)
    (manifest_dir / "plugin.json").write_text(json_text(manifest), encoding="utf-8")
    (destination / "README.md").write_text(
        "# dev for Codex\n\n"
        "Generated from `dev/` by `scripts/build_codex_plugin.py`. "
        "Do not edit this directory directly.\n",
        encoding="utf-8",
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != ".DS_Store"
    }


def check() -> int:
    with tempfile.TemporaryDirectory(prefix="codex-plugin-") as temp:
        expected = Path(temp) / "dev"
        build(expected)
        actual_files = snapshot(DESTINATION) if DESTINATION.is_dir() else {}
        expected_files = snapshot(expected)

    missing = sorted(expected_files.keys() - actual_files.keys())
    extra = sorted(actual_files.keys() - expected_files.keys())
    changed = sorted(
        path
        for path in expected_files.keys() & actual_files.keys()
        if expected_files[path] != actual_files[path]
    )
    if not (missing or extra or changed):
        print("Codex plugin is up to date.")
        return 0
    for label, paths in (("missing", missing), ("extra", extra), ("changed", changed)):
        for path in paths:
            print(f"{label}: plugins/dev/{path}")
    print("Run: python3 scripts/build_codex_plugin.py")
    return 1


def main() -> int:
    args = parse_args()
    if args.check:
        return check()

    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".dev-build-", dir=DESTINATION.parent
    ) as temp:
        built = Path(temp) / "dev"
        build(built)
        if DESTINATION.exists():
            shutil.rmtree(DESTINATION)
        shutil.copytree(built, DESTINATION)
    print(f"Built Codex plugin: {DESTINATION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
