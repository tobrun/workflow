import unittest
from unittest import mock

from runner import worktree as wt
from runner.tests.helpers import FactoryTestCase, git, make_repo


class WorktreeTests(FactoryTestCase):
    def setUp(self):
        super().setUp()
        self.repo = make_repo(self.root)

    def test_default_branch_from_remote_head(self):
        git(self.repo, "checkout", "--quiet", "-b", "feature")
        self.assertEqual(wt.default_branch(self.repo, "origin"), "main")

    def test_default_branch_falls_back_to_current(self):
        git(self.repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
        git(self.repo, "checkout", "--quiet", "-b", "trunk")
        self.assertEqual(wt.default_branch(self.repo, "origin"), "trunk")

    def test_create_records_base_and_branch(self):
        created = wt.create(self.repo, self.root / "run" / "worktree", "webhook")
        self.assertEqual(created.base, "main")
        self.assertEqual(created.branch, "factory/webhook")
        self.assertEqual(created.base_sha, git(self.repo, "rev-parse", "origin/main"))
        self.assertEqual(git(created.worktree, "rev-parse", "--abbrev-ref", "HEAD"), "factory/webhook")
        self.assertIn(created.worktree, wt.registered_worktrees(self.repo))

    def test_explicit_base(self):
        git(self.repo, "checkout", "--quiet", "-b", "release")
        (self.repo / "release.txt").write_text("r\n")
        git(self.repo, "add", "release.txt")
        git(self.repo, "commit", "--quiet", "-m", "release")
        git(self.repo, "push", "--quiet", "-u", "origin", "release")
        created = wt.create(self.repo, self.root / "wt", "rel", base="release")
        self.assertEqual(created.base, "release")
        self.assertTrue((created.worktree / "release.txt").exists())

    def test_branch_suffixes_on_collision(self):
        git(self.repo, "branch", "factory/webhook")
        git(self.repo, "push", "--quiet", "origin", "main:factory/webhook-2")
        created = wt.create(self.repo, self.root / "wt", "webhook")
        self.assertEqual(created.branch, "factory/webhook-3")

    def test_excludes_go_to_the_common_git_dir_once(self):
        created = wt.create(self.repo, self.root / "wt1", "one")
        wt.create(created.worktree, self.root / "wt2", "two")
        exclude = self.repo / ".git" / "info" / "exclude"
        text = exclude.read_text()
        for pattern in wt.EXCLUDE_PATTERNS:
            self.assertEqual(text.splitlines().count(pattern), 1)
        self.assertFalse((created.worktree / ".git" / "info").exists())
        self.assertEqual(wt.common_dir(created.worktree), (self.repo / ".git").resolve())
        plan = created.worktree / ".dev" / "one"
        plan.mkdir(parents=True)
        (plan / "build-result.json").write_text("{}")
        (created.worktree / ".dev" / "factory-run.json").write_text("{}")
        (created.worktree / ".dev" / ".metrics").mkdir()
        (created.worktree / ".dev" / ".metrics" / "build.json").write_text("{}")
        self.assertEqual(wt.changed_paths(created.worktree), [])

    def test_remove_worktree_and_branch(self):
        created = wt.create(self.repo, self.root / "wt", "gone")
        done = wt.remove(self.repo, created.worktree, created.branch)
        self.assertFalse(created.worktree.exists())
        self.assertNotIn(created.worktree, wt.registered_worktrees(self.repo))
        self.assertEqual(git(self.repo, "branch", "--list", "factory/gone"), "")
        self.assertEqual(len(done), 2)

    def test_missing_remote_has_repair_hint(self):
        git(self.repo, "remote", "remove", "origin")
        with self.assertRaises(wt.GitError) as caught:
            wt.create(self.repo, self.root / "wt", "x")
        self.assertIn("remote add origin", caught.exception.repair)

    def test_rollback_removes_only_unregistered_partial_worktree(self):
        target = self.root / "wt"
        real_git = wt.git

        def failing(*args, **kwargs):
            if "worktree" in args and "add" in args:
                target.mkdir()
                (target / "partial.txt").write_text("half")
                raise wt.GitError("simulated add failure")
            return real_git(*args, **kwargs)

        with mock.patch("runner.worktree.git", side_effect=failing):
            with self.assertRaises(wt.GitError) as caught:
                wt.create(self.repo, target, "broken")
        self.assertFalse(target.exists())
        self.assertIsNotNone(caught.exception.repair)

    def test_cleanup_keeps_registered_worktree(self):
        created = wt.create(self.repo, self.root / "wt", "keep")
        self.assertFalse(wt.cleanup_partial(self.repo, created.worktree))
        self.assertTrue(created.worktree.exists())

    def test_commit_paths_only_stages_named_paths(self):
        created = wt.create(self.repo, self.root / "wt", "paths")
        (created.worktree / "a.txt").write_text("a")
        (created.worktree / "b.txt").write_text("b")
        sha = wt.commit_paths(created.worktree, ["a.txt"], "docs(scope): spec for paths")
        self.assertIsNotNone(sha)
        self.assertEqual(git(created.worktree, "show", "--name-only", "--format=", "HEAD"), "a.txt")
        body = git(created.worktree, "log", "-1", "--format=%B")
        self.assertNotIn("Co-Authored-By", body)
        self.assertIsNone(wt.commit_paths(created.worktree, ["a.txt"], "again"))


if __name__ == "__main__":
    unittest.main()
