import json
import unittest

from runner import requests
from runner.model import Run
from runner.tests.helpers import FactoryTestCase, make_repo

ISSUE = {
    "key": "PROJ-42",
    "self": "https://jira-prod-eu.prod.atl-paas.net/rest/api/3/issue/10042",
    "fields": {
        "summary": "Deduplicate webhook deliveries",
        "issuetype": {"name": "Story"},
        "status": {"name": "To Do"},
        "priority": {"name": "P1"},
        "labels": ["payments", "reliability"],
        "parent": {"key": "PROJ-7", "fields": {"summary": "Webhook hardening"}},
        "description": {"type": "doc", "version": 1, "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Problem"}]},
            {"type": "paragraph", "content": [
                {"type": "text", "text": "The provider retries "},
                {"type": "text", "text": "invoice.paid", "marks": [{"type": "code"}]},
                {"type": "text", "text": " and we "},
                {"type": "text", "text": "double charge", "marks": [{"type": "strong"}]},
                {"type": "text", "text": "."},
            ]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "seen in prod"}]}]},
                {"type": "listItem", "content": [{"type": "paragraph", "content": [
                    {"type": "text", "text": "runbook", "marks": [{"type": "link", "attrs": {"href": "https://wiki/runbook"}}]}]}]},
            ]},
            {"type": "codeBlock", "attrs": {"language": "json"}, "content": [{"type": "text", "text": "{\"id\": 1}"}]},
            {"type": "table", "content": [
                {"type": "tableRow", "content": [
                    {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Env"}]}]},
                    {"type": "tableHeader", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Rate"}]}]}]},
                {"type": "tableRow", "content": [
                    {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "prod"}]}]},
                    {"type": "tableCell", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "2%"}]}]}]},
            ]},
            {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@alex"}},
                                              {"type": "text", "text": " owns the handler"}]},
        ]},
        "comment": {"comments": [
            {"author": {"displayName": "Sam"}, "created": "2026-09-01T10:00:00.000+0000",
             "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Use the event id."}]}]}},
        ]},
    },
}


class SourceTests(FactoryTestCase):
    def acli_state(self, **state) -> None:
        (self.root / "acli-state.json").write_text(json.dumps({"auth": True, "site": "acme.atlassian.net",
                                                               "issues": {"PROJ-42": ISSUE}, **state}))

    def test_text(self):
        request = requests.from_text("  Add idempotency protection  ")
        self.assertEqual((request.title, request.body, request.source), ("Add idempotency protection",
                                                                         "Add idempotency protection", {"kind": "text"}))
        with self.assertRaises(requests.RequestError):
            requests.from_text("   ")

    def test_file(self):
        path = self.root / "request.md"
        path.write_text("\n# Deduplicate webhooks\n\nThe provider retries deliveries.\n")
        request = requests.from_file(path)
        self.assertEqual(request.title, "Deduplicate webhooks")
        self.assertTrue(request.body.startswith("# Deduplicate webhooks"))
        self.assertEqual(request.source, {"kind": "file", "path": str(path.resolve())})

    def test_file_errors(self):
        empty = self.root / "empty.md"
        empty.write_text("\n\n")
        huge = self.root / "huge.md"
        huge.write_text("x" * (requests.MAX_FILE_BYTES + 1))
        binary = self.root / "binary.bin"
        binary.write_bytes(b"\xff\xfe\x00")
        for path, text in ((self.root / "missing.md", "does not exist"), (self.root, "not a file"),
                           (empty, "is empty"), (huge, "the limit is"), (binary, "not UTF-8")):
            with self.subTest(path=path.name), self.assertRaisesRegex(requests.RequestError, text):
                requests.from_file(path)

    def test_jira_renders_markdown_with_site_link(self):
        self.acli_state()
        request = requests.from_jira("proj-42")
        self.assertEqual(request.title, "PROJ-42: Deduplicate webhook deliveries")
        self.assertEqual(request.source, {"kind": "jira", "key": "PROJ-42",
                                          "url": "https://acme.atlassian.net/browse/PROJ-42",
                                          "summary": "Deduplicate webhook deliveries"})
        body = request.body
        for expected in (
            "# PROJ-42: Deduplicate webhook deliveries",
            "- Source: Jira PROJ-42 (https://acme.atlassian.net/browse/PROJ-42)",
            "- Type: Story | Status: To Do | Priority: P1",
            "- Parent: PROJ-7 - Webhook hardening",
            "- Labels: payments, reliability",
            "## Problem",
            "The provider retries `invoice.paid` and we **double charge**.",
            "- seen in prod\n- [runbook](https://wiki/runbook)",
            "```json\n{\"id\": 1}\n```",
            "| Env | Rate |\n| --- | --- |\n| prod | 2% |",
            "@alex owns the handler",
            "### Sam - 2026-09-01",
            "Use the event id.",
        ):
            self.assertIn(expected, body)
        calls = self.stub_calls("acli")
        self.assertEqual(calls[1]["argv"][:4], ["jira", "workitem", "view", "PROJ-42"])

    def test_jira_errors(self):
        with self.assertRaisesRegex(requests.RequestError, "not a Jira key"):
            requests.from_jira("not a key")
        self.acli_state(auth=False)
        with self.assertRaises(requests.RequestError) as caught:
            requests.from_jira("PROJ-42")
        self.assertIn("not authenticated", str(caught.exception))
        self.assertEqual(caught.exception.repair, "acli jira auth login")
        self.acli_state()
        with self.assertRaisesRegex(requests.RequestError, "could not fetch PROJ-999"):
            requests.from_jira("PROJ-999")

    def test_plain_string_description_and_no_description(self):
        issue = json.loads(json.dumps(ISSUE))
        issue["fields"]["description"] = "plain text body"
        self.assertIn("plain text body", requests.jira_request(issue, "PROJ-42").body)
        issue["fields"]["description"] = None
        issue["fields"]["comment"] = {"comments": []}
        body = requests.jira_request(issue, "PROJ-42").body
        self.assertIn("(no description)", body)
        self.assertNotIn("## Comments", body)


class NewFromSourceTests(FactoryTestCase):
    def call(self, *argv):
        import contextlib
        import io
        from runner import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_new_from_jira_ticket(self):
        (self.root / "acli-state.json").write_text(json.dumps({"issues": {"PROJ-42": ISSUE}}))
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "--jira", "PROJ-42", "--yes")
        self.assertEqual(code, 0, stdout)
        run = Run.load(next((self.home / "runs").iterdir()))
        self.assertEqual(run.plan, "proj-42-deduplicate-webhook-deliveries")
        self.assertEqual(run.data["branch"], "factory/proj-42-deduplicate-webhook-deliveries")
        self.assertEqual(run.data["request"], "PROJ-42: Deduplicate webhook deliveries")
        self.assertEqual(run.data["source"]["key"], "PROJ-42")
        request_file = run.dir / "request.md"
        self.assertEqual(run.data["request_file"], str(request_file))
        self.assertIn("## Description", request_file.read_text())
        launch = self.stub_calls("claude")[0]
        self.assertEqual(launch["argv"][0],
                         f"/factory:scope PROJ-42: Deduplicate webhook deliveries (full request: {request_file})")
        self.assertIn("--add-dir", launch["argv"])
        self.assertEqual(launch["argv"][launch["argv"].index("--add-dir") + 1], str(run.dir))
        self.wait_status(run.dir, ("done", "needs-human", "cancelled"))

    def test_new_from_file(self):
        path = self.root / "ask.md"
        path.write_text("# Deduplicate webhooks\n\nLong context about the provider...\n")
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "--file", str(path), "--yes")
        self.assertEqual(code, 0, stdout)
        run = Run.load(next((self.home / "runs").iterdir()))
        self.assertEqual(run.plan, "deduplicate-webhooks")
        self.assertEqual((run.dir / "request.md").read_text(), path.read_text())
        self.assertEqual(run.data["source"], {"kind": "file", "path": str(path.resolve())})
        context = json.loads((run.worktree / ".dev" / "factory-run.json").read_text())
        self.assertEqual(context["request_file"], str(run.dir / "request.md"))
        self.wait_status(run.dir, ("done", "needs-human", "cancelled"))

    def test_exactly_one_source(self):
        repo = make_repo(self.root)
        path = self.root / "ask.md"
        path.write_text("x\n")
        for argv, text in ((["new", str(repo)], "give exactly one change request"),
                           (["new", str(repo), "text", "--file", str(path)], "got request, --file"),
                           (["new", str(repo), "--file", str(path), "--jira", "PROJ-1"], "got --file, --jira")):
            with self.subTest(argv=argv):
                code, _, err = self.call(*argv)
                self.assertEqual(code, 2)
                self.assertIn(text, err)
        self.assertFalse(any((self.home / "runs").glob("*")) if (self.home / "runs").exists() else False)

    def test_unreadable_source_creates_no_run(self):
        (self.root / "acli-state.json").write_text(json.dumps({"auth": False}))
        repo = make_repo(self.root)
        code, stdout, _ = self.call("new", str(repo), "--jira", "PROJ-42")
        self.assertEqual(code, 2)
        self.assertIn("Could not read the change request: acli is not authenticated", stdout)
        self.assertIn("> acli jira auth login", stdout)
        self.assertEqual(list((self.home / "runs").glob("*")), [])


if __name__ == "__main__":
    unittest.main()
