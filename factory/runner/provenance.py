"""Runtime provenance: which runner, skills, hosts, model, contract, and configuration an attempt really used.

Codex does not run a plugin from the source path `codex plugin list` prints: it installs a
copy under `$CODEX_HOME/plugins/cache/{marketplace}/{plugin}/{version}/` and loads that.
A checkout edited after the install therefore runs stale skills under the same name and
version, so identity here is always a content hash of the files that execute.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from runner import FACTORY_ROOT, config, records
from runner.model import utc_now

REPO_ROOT = FACTORY_ROOT.parent
GENERATED = REPO_ROOT / "plugins" / "factory"
RUNNER_TREES = ("runner", "scripts", "skills", "references", "bin")
JUNK = {"__pycache__", ".DS_Store"}
PLUGIN_LINE = re.compile(r"^factory@(\S+)\s+(installed|not installed)[^\n]*?\s(\d+\.\d+\.\d+\S*)?\s+(/\S.*)$", re.M)


def tree_sha256(root: Path, *, skip_tests: bool = False) -> str | None:
    """A content hash over every file's relative path and bytes; None when the directory is missing."""
    root = Path(root)
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if not path.is_file() or JUNK & set(relative.parts) or (skip_tests and "tests" in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    return digest.hexdigest()


def run_quiet(argv: list[str], timeout: float = 30, cwd: Path | None = None) -> tuple[int, str]:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd=cwd)
    except (OSError, subprocess.TimeoutExpired) as error:
        return 127, str(error)
    return result.returncode, (result.stdout + result.stderr).strip()


def runner_identity() -> dict:
    """The runner checkout, including uncommitted edits: a content hash beside the Git revision."""
    digest = hashlib.sha256()
    for tree in RUNNER_TREES:
        digest.update(f"{tree}:{tree_sha256(FACTORY_ROOT / tree, skip_tests=True)}\n".encode("utf-8"))
    code, revision = run_quiet(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"])
    code_status, status = run_quiet(["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--", "factory", "plugins"])
    manifest = FACTORY_ROOT / ".claude-plugin" / "plugin.json"
    version = json.loads(manifest.read_text(encoding="utf-8")).get("version") if manifest.is_file() else None
    return {
        "version": version,
        "content_sha256": digest.hexdigest(),
        "revision": revision if code == 0 else None,
        "uncommitted_changes": bool(status) if code_status == 0 else None,
    }


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def skill_bundles(plugin_list: str | None = None, *, root: Path | None = None) -> dict:
    """The generated bundle beside the one Codex will actually load, and whether they are the same files.

    `root` is the checkout whose generated tree counts; it defaults to this runner's own.
    """
    if plugin_list is None:
        code, plugin_list = run_quiet([config.binary("codex"), "plugin", "list"], timeout=60)
        if code != 0:
            plugin_list = ""
    tree = Path(root) / "plugins" / "factory" if root else GENERATED
    generated = {"path": str(tree), "sha256": tree_sha256(tree)}
    installed: dict = {"listed": False}
    match = PLUGIN_LINE.search(plugin_list or "")
    if match and match.group(2) == "installed":
        marketplace, version, source = match.group(1), match.group(3), match.group(4).strip()
        cache = codex_home() / "plugins" / "cache" / marketplace / "factory" / (version or "")
        loaded = cache if version and cache.is_dir() else Path(source)
        installed = {"listed": True, "marketplace": marketplace, "version": version, "listed_source": source,
                     "path": str(loaded), "sha256": tree_sha256(loaded)}
    same = None
    if installed.get("sha256") and generated["sha256"]:
        same = installed["sha256"] == generated["sha256"]
    forced = os.environ.get("FACTORY_DIRECT_SKILL_PATH", "") not in ("", "0", "false")
    fallback = None
    if not forced and not installed.get("listed"):
        fallback = "the Codex factory plugin is not installed"
    elif not forced and same is False:
        fallback = f"the installed Codex factory plugin ({installed['path']}) differs from the generated skills"
    # A missing or stale install never stops a run: agents read the generated skills by path instead.
    direct = forced or fallback is not None
    resolved = generated if direct else installed
    label = resolved.get("sha256")
    return {
        "generated": generated,
        "installed": installed,
        "installed_matches_generated": same,
        "resolution": "direct-path" if direct else "installed-plugin",
        "fallback": fallback,
        "skills_id": f"sha256:{label}" if label else "unverified",
    }


FOREMAN_SKILL = Path("skills") / "foreman" / "SKILL.md"
PROTOCOL = Path("references") / "factory-run.md"


def tree_policy(tree: Path) -> dict:
    """The foreman policy a plugin tree carries: its skill, the protocol reference it links, and one hash over both.

    The hash covers the two files' contents, so a change to either one is a different policy.
    """
    skill, references = Path(tree) / FOREMAN_SKILL, Path(tree) / PROTOCOL
    digest = hashlib.sha256()
    for path in (skill, references):
        content = path.read_bytes() if path.is_file() else b""
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii") + b"\n")
    return {"skill": str(skill), "references": str(references), "sha256": digest.hexdigest()}


def policy_identity(bundles: dict | None = None, *, root: Path | None = None) -> dict:
    """The policy the foreman reads: the installed plugin's copy when Codex loads it, else the generated tree."""
    installed = (bundles or {}).get("installed") or {}
    if (bundles or {}).get("resolution") == "installed-plugin" and installed.get("path"):
        return tree_policy(Path(installed["path"]))
    return tree_policy(Path(root or REPO_ROOT) / "plugins" / "factory")


def generated_is_current() -> tuple[bool, str]:
    code, output = run_quiet([sys.executable, str(REPO_ROOT / "scripts" / "build_codex_plugin.py"), "--check",
                              "--plugin", "factory"], timeout=120)
    return code == 0, output


_HOSTS: dict | None = None


def host_versions() -> dict:
    """Versions of the tools an attempt depends on; read once per process."""
    global _HOSTS
    if _HOSTS is None:
        versions = {}
        for name, argv in (("codex", [config.binary("codex"), "--version"]), ("git", [config.binary("git"), "--version"]),
                           ("gh", [config.binary("gh"), "--version"])):
            code, output = run_quiet(argv)
            versions[name] = output.splitlines()[0] if code == 0 and output else None
        versions["python"] = platform.python_version()
        versions["platform"] = f"{sys.platform} {platform.release()} {platform.machine()}"
        _HOSTS = versions
    return dict(_HOSTS)


# Settings that change what an attempt means are read once per attempt; resource controls stay live.
SEMANTIC_SETTINGS = ("max_retries", "stop_on_repeated_reason", "foreman", "foreman_model", "foreman_effort",
                     "max_stage_attempts", "max_repairs_per_stage", "max_wait_minutes", "max_run_hours",
                     "max_overrides_per_run")
LIVE_SETTINGS = ("max_concurrent_stages", "stage_poll_seconds", "heartbeat_seconds", "notify")


def effective_config(cfg: config.Config, repo: str) -> dict:
    values = asdict(cfg)
    values.pop("raw", None)
    repos = values.pop("repos", {})
    values["repo"] = repos.get(config.normalize_repo(repo), {})
    values["codex_sandbox"] = cfg.sandbox_for(repo)
    return {"values": values, "sha256": records.canonical_sha256(values),
            "semantic": {key: values[key] for key in SEMANTIC_SETTINGS},
            "live": list(LIVE_SETTINGS)}


def environment_descriptor(contract: dict | None, env: dict, *, forwarded_github_token: bool) -> dict:
    """Names and whether they are set; never a value."""
    names: set[str] = set()
    if contract:
        names |= set(contract.get("environment", {}).get("required", []))
        for section in ("setup", "validation"):
            for command in contract.get(section, []):
                names |= set(command.get("env", []))
    return {"required": {name: "set" if env.get(name) else "unset" for name in sorted(names)},
            "github_token": "forwarded" if forwarded_github_token else ("inherited" if env.get("GH_TOKEN") or
                                                                         env.get("GITHUB_TOKEN") else "absent")}


def attempt_manifest(*, run_id: str, stage: str, attempt: int, model: str, effort: str, timeout_s: int | None,
                     cfg: config.Config, repo: str, contract: dict | None, contract_sha256: str | None,
                     env: dict, forwarded_github_token: bool, bundles: dict | None = None) -> dict:
    return {
        "schema": "factory.runtime/1",
        "run_id": run_id, "stage": stage, "attempt": attempt, "recorded_at": utc_now(),
        "runner": runner_identity(),
        "skills": bundles if bundles is not None else skill_bundles(),
        "hosts": host_versions(),
        "model": model, "effort": effort, "timeout_s": timeout_s,
        "contract_sha256": contract_sha256,
        "config": effective_config(cfg, repo),
        "environment": environment_descriptor(contract, env, forwarded_github_token=forwarded_github_token),
    }
