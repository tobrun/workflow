"""Git branch and worktree lifecycle for one run."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from runner.config import binary

EXCLUDE_MARKER = "# factory: plan directories, run files, metrics, and installed dependencies are never committed"
EXCLUDE_PATTERNS = (".dev/",)


class GitError(Exception):
    def __init__(self, message: str, repair: str | None = None):
        super().__init__(message)
        self.repair = repair


# The runner's own Git calls never run repository hooks or an fsmonitor command: an agent (or a
# repository) could otherwise make the runner execute code outside any sandbox.
SAFE = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


def git(*args: str, cwd: Path | str | None = None, check: bool = True, timeout: float | None = 300,
        strip: bool = True) -> str:
    try:
        result = subprocess.run([binary("git"), *SAFE, *args], cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, stdin=subprocess.DEVNULL)
    except FileNotFoundError as error:
        raise GitError("git is not installed", "install git") from error
    except subprocess.TimeoutExpired as error:
        raise GitError(f"git {' '.join(args)} timed out") from error
    if check and result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout.strip() if strip else result.stdout


def git_ok(*args: str, cwd: Path | str | None = None) -> bool:
    try:
        return subprocess.run([binary("git"), *SAFE, *args], cwd=cwd, capture_output=True,
                              stdin=subprocess.DEVNULL).returncode == 0
    except FileNotFoundError:
        return False


def toplevel(repo: str | Path) -> Path:
    path = Path(repo).expanduser()
    if not path.is_dir():
        raise GitError(f"{path} is not a directory")
    return Path(git("-C", str(path), "rev-parse", "--show-toplevel"))


def common_dir(repo: Path) -> Path:
    value = git("-C", str(repo), "rev-parse", "--git-common-dir")
    path = Path(value)
    return (path if path.is_absolute() else Path(repo) / path).resolve()


def require_remote(repo: Path, remote: str) -> None:
    if not git_ok("-C", str(repo), "remote", "get-url", remote):
        raise GitError(f"repository has no remote named {remote!r}",
                       f"git -C {repo} remote add {remote} <url>")


def fetch(repo: Path, remote: str) -> None:
    git("-C", str(repo), "fetch", "--quiet", remote, timeout=600)


def default_branch(repo: Path, remote: str) -> str:
    ref = git("-C", str(repo), "symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD", check=False)
    prefix = f"refs/remotes/{remote}/"
    if ref.startswith(prefix):
        return ref[len(prefix):]
    current = git("-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current:
        return current
    configured = git("-C", str(repo), "config", "init.defaultBranch", check=False)
    return configured or "main"


def resolve_base(repo: Path, remote: str, base: str) -> str:
    for ref in (f"refs/remotes/{remote}/{base}", f"refs/heads/{base}", base):
        sha = git("-C", str(repo), "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
        if sha:
            return sha
    raise GitError(f"base {base!r} does not resolve in {repo}", f"git -C {repo} fetch {remote}")


def branch_exists(repo: Path, remote: str, branch: str) -> bool:
    return any(
        git_ok("-C", str(repo), "show-ref", "--verify", "--quiet", ref)
        for ref in (f"refs/heads/{branch}", f"refs/remotes/{remote}/{branch}")
    )


def unique_branch(repo: Path, remote: str, plan: str) -> str:
    candidate, suffix = f"factory/{plan}", 1
    while branch_exists(repo, remote, candidate):
        suffix += 1
        candidate = f"factory/{plan}-{suffix}"
    return candidate


def install_excludes(repo: Path, patterns: tuple[str, ...] | list[str] = EXCLUDE_PATTERNS) -> Path:
    """Append `patterns` to the repository's info/exclude under the factory marker, once each."""
    exclude = common_dir(repo) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    lines = existing.splitlines()
    missing = [pattern for pattern in patterns if pattern not in lines]
    if missing:
        block = ([] if EXCLUDE_MARKER in lines else [EXCLUDE_MARKER]) + missing
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(prefix + "\n".join(block) + "\n")
    return exclude


def exclude_pattern(worktree: Path, path: Path) -> str:
    """The info/exclude line that hides one installed path everywhere in the repository."""
    relative = Path(os.path.realpath(path)).relative_to(Path(os.path.realpath(worktree))).as_posix()
    return f"/{relative}/" if path.is_dir() else f"/{relative}"


def registered_worktrees(repo: Path) -> list[Path]:
    output = git("-C", str(repo), "worktree", "list", "--porcelain", check=False)
    return [Path(line[len("worktree "):]).resolve() for line in output.splitlines() if line.startswith("worktree ")]


@dataclass
class Created:
    repo: Path
    base: str
    base_sha: str
    branch: str
    worktree: Path


def create(repo: str | Path, worktree: Path, plan: str, *, remote: str = "origin", base: str | None = None) -> Created:
    """Fetch, pick base and branch, and add a linked worktree. Raises GitError with a repair hint."""
    root = toplevel(repo)
    require_remote(root, remote)
    try:
        fetch(root, remote)
    except GitError as error:
        raise GitError(str(error), f"git -C {root} fetch {remote}") from error
    base_name = base or default_branch(root, remote)
    base_sha = resolve_base(root, remote, base_name)
    branch = unique_branch(root, remote, plan)
    try:
        git("-C", str(root), "worktree", "add", "--quiet", "-b", branch, str(worktree), base_sha)
    except GitError as error:
        cleanup_partial(root, worktree)
        raise GitError(str(error), f"git -C {root} worktree prune && factory rm <run-id> --force") from error
    install_excludes(root)
    (common_dir(root) / "logs").mkdir(exist_ok=True)
    configure_upstream(worktree, remote, branch)
    return Created(root, base_name, base_sha, branch, worktree.resolve())


def cleanup_partial(repo: Path, worktree: Path) -> bool:
    """Remove only an empty or provably unregistered partial worktree directory."""
    if not worktree.exists():
        return True
    if worktree.resolve() in registered_worktrees(repo):
        return False
    if worktree.is_dir() and not any(worktree.iterdir()):
        worktree.rmdir()
        return True
    if not (worktree / ".git").exists():
        shutil.rmtree(worktree)
        return True
    return False


def remove(repo: Path, worktree: Path, branch: str | None) -> list[str]:
    """Force-remove a run's worktree and local branch; returns what was done."""
    done: list[str] = []
    if worktree.exists() or worktree.resolve() in registered_worktrees(repo):
        git("-C", str(repo), "worktree", "remove", "--force", str(worktree), check=False)
        done.append(f"removed worktree {worktree}")
    if worktree.exists() and worktree.resolve() not in registered_worktrees(repo):
        shutil.rmtree(worktree, ignore_errors=True)
    if branch and git_ok("-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"):
        git("-C", str(repo), "branch", "-D", branch)
        done.append(f"deleted branch {branch}")
    git("-C", str(repo), "worktree", "prune", check=False)
    return done


# --- state inspection used by gates -------------------------------------------

def dirty_tracked(worktree: Path) -> list[str]:
    output = git("-C", str(worktree), "status", "--porcelain", "--untracked-files=no", strip=False)
    return [line[3:] for line in output.splitlines() if line.strip()]


def changed_paths(worktree: Path) -> list[str]:
    """Modified, staged, and untracked paths, honoring excludes."""
    output = git("-C", str(worktree), "status", "--porcelain", "--untracked-files=all", "-z", strip=False)
    paths: list[str] = []
    entries = output.split("\0")
    index = 0
    while index < len(entries):
        entry = entries[index]
        index += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in "RC":
            index += 1  # the original path follows a rename
        paths.append(path)
    return paths


def head(worktree: Path) -> str:
    return git("-C", str(worktree), "rev-parse", "HEAD")


def current_branch(worktree: Path) -> str | None:
    """The checked-out branch, or None when HEAD is detached."""
    return git("-C", str(worktree), "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or None


def is_ancestor(worktree: Path, ancestor: str, descendant: str = "HEAD") -> bool:
    return git_ok("-C", str(worktree), "merge-base", "--is-ancestor", ancestor, descendant)


def committed_paths(worktree: Path, baseline: str) -> set[str]:
    output = git("-C", str(worktree), "diff", "--name-only", "--no-renames", "-z", baseline, "HEAD", strip=False)
    return {path for path in output.split("\0") if path}


def stage_delta(worktree: Path, baseline: str) -> list[tuple[str, str]]:
    """Every path changed since `baseline`: committed, staged, unstaged, deleted, both rename sides, and untracked.

    Returns (status, path) with status A, M, D, T, or ? (untracked), honoring excludes for untracked files.
    """
    changes: dict[str, str] = {}
    output = git("-C", str(worktree), "diff", "--name-status", "--no-renames", "-z", baseline, strip=False)
    fields = [field for field in output.split("\0") if field]
    for status, path in zip(fields[0::2], fields[1::2]):
        changes[path] = status[:1]
    untracked = git("-C", str(worktree), "ls-files", "--others", "--exclude-standard", "-z", strip=False)
    for path in filter(None, untracked.split("\0")):
        changes.setdefault(path, "?")
    return sorted((status, path) for path, status in changes.items())


def tracked_under(worktree: Path, prefix: str, ref: str | None = None) -> list[str]:
    """Tracked paths under a directory, in the index or at `ref`."""
    if ref is None:
        output = git("-C", str(worktree), "ls-files", "-z", "--", prefix, strip=False)
    else:
        output = git("-C", str(worktree), "ls-tree", "-r", "-z", "--name-only", ref, "--", prefix, strip=False)
    return [path for path in output.split("\0") if path]


def configure_upstream(worktree: Path, remote: str, branch: str) -> None:
    """Record the run branch's upstream once, so a later `git push` needs no write to the shared Git config."""
    git("-C", str(worktree), "config", f"branch.{branch}.remote", remote)
    git("-C", str(worktree), "config", f"branch.{branch}.merge", f"refs/heads/{branch}")


def sandbox_git_dirs(worktree: Path) -> list[Path]:
    """The parts of a linked worktree's Git metadata a stage needs to commit, fetch, and push.

    Deliberately not the common directory itself: its config, hooks, and info/exclude
    steer Git commands the runner later runs outside any sandbox.
    """
    common = common_dir(worktree)
    own = Path(git("-C", str(worktree), "rev-parse", "--absolute-git-dir"))
    return [common / "objects", common / "refs", common / "logs", own]


def commits_after(worktree: Path, base_sha: str) -> int:
    count = git("-C", str(worktree), "rev-list", "--count", f"{base_sha}..HEAD")
    return int(count or 0)


def commit_paths(worktree: Path, paths: list[str], message: str) -> str | None:
    """Stage exactly these paths and commit as the configured user. None when nothing changed."""
    if not paths:
        return None
    git("-C", str(worktree), "add", "--", *paths)
    if git_ok("-C", str(worktree), "diff", "--cached", "--quiet"):
        return None
    git("-C", str(worktree), "commit", "--quiet", "-m", message)
    return git("-C", str(worktree), "rev-parse", "HEAD")


def unpushed(worktree: Path, remote: str, branch: str, *, fetch: bool = True) -> int | None:
    """Commits on HEAD missing from the remote branch; None when the branch is not on the remote."""
    if fetch:
        git("-C", str(worktree), "fetch", "--quiet", remote, check=False, timeout=600)
    ref = f"refs/remotes/{remote}/{branch}"
    if not git_ok("-C", str(worktree), "show-ref", "--verify", "--quiet", ref):
        return None
    return int(git("-C", str(worktree), "rev-list", "--count", f"{ref}..HEAD") or 0)


def safe_ref_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._/-]+", value)) and ".." not in value and not value.startswith("-")


def path_is_within(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.realpath(child)).relative_to(os.path.realpath(parent))
        return True
    except ValueError:
        return False
