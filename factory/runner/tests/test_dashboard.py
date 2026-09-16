import contextlib
import io
import json
import os
import re
import stat
import unittest
from pathlib import Path

from runner import cli, dashboard, events, notify, slots, supervise
from runner.model import Run
from runner.tests.helpers import FactoryTestCase


def data_of(html: str) -> dict:
    match = re.search(r'<script id="factory-data" type="application/json">(.*?)</script>', html, re.S)
    return json.loads(match.group(1))


class DashboardTestCase(FactoryTestCase):
    def fixture(self, status: str, stage: str = "build", plan: str = "thing", reason: str | None = None,
                pr: dict | None = None) -> Run:
        run = Run.create(self.home / "runs", repo="/tmp/project", request="x", plan=plan)
        run.data["status"] = status
        run.data["stage"] = stage
        run.data["branch"] = f"factory/{plan}"
        if reason:
            run.data["human"]["reason"] = reason
        if pr:
            run.data["pr"].update(pr)
        run.save()
        return run


class GroupingTests(DashboardTestCase):
    def test_groups_and_actionable_ordering(self):
        live = self.fixture("running", plan="live")
        lock = supervise.WorkerLock(live.dir)
        self.assertTrue(lock.acquire())
        (live.dir / "worker.heartbeat").write_text("now")
        try:
            self.fixture("scoping", "scope", plan="scoping")
            self.fixture("queued", plan="dead-worker")
            self.fixture("done", "ship", plan="shipped", pr={"number": 7, "url": "https://github.com/o/r/pull/7",
                                                            "draft": False})
            exhausted = self.fixture("cancelled", plan="cancelled", reason="retry budget exhausted (5/5)")
            exhausted.data["retries"]["used"] = 5
            exhausted.save()
            self.fixture("needs-human", plan="needs", reason="premise invalidated")
            rows = dashboard.summaries(self.home, heartbeat_seconds=30)
        finally:
            lock.release()
        order = [(row["id"].split("-", 2)[2], row["group"]) for row in rows]
        self.assertEqual(order, [
            ("needs", "needs"), ("cancelled", "needs"), ("dead-worker", "needs"),
            ("live", "flight"), ("scoping", "flight"), ("shipped", "done"),
        ])
        dead = next(r for r in rows if r["id"].endswith("dead-worker"))
        self.assertTrue(dead["stale"])
        self.assertEqual(dead["next"], f"factory resume {dead['id']}")
        cancelled = next(r for r in rows if r["id"].endswith("cancelled"))
        self.assertEqual(cancelled["next"], f"factory retry {cancelled['id']} --reset-budget")

    def test_stale_heartbeat_counts_as_needs_attention(self):
        run = self.fixture("running", plan="hung")
        lock = supervise.WorkerLock(run.dir)
        self.assertTrue(lock.acquire())
        try:
            beat = run.dir / "worker.heartbeat"
            beat.write_text("old")
            os.utime(beat, (0, 0))
            row = dashboard.summarize(run.dir, run, None, heartbeat_seconds=30)
        finally:
            lock.release()
        self.assertTrue(row["worker_alive"])
        self.assertTrue(row["stale"])
        self.assertEqual(row["group"], "needs")


class RenderTests(DashboardTestCase):
    def test_html_escaping(self):
        hostile = '</script><img src=x onerror="alert(1)"> & <b>'
        self.fixture("needs-human", reason=hostile)
        path = dashboard.regenerate(self.home, heartbeat_seconds=30)
        html = path.read_text()
        self.assertNotIn("<img src=x", html)
        self.assertEqual(html.count("</script>"), 2)
        self.assertEqual(data_of(html)["runs"][0]["blocker"], hostile)
        self.assertIn('<meta http-equiv="refresh" content="30">', html)

    def test_data_block_replacement(self):
        self.fixture("queued", plan="first")
        path = dashboard.regenerate(self.home, heartbeat_seconds=30)
        self.assertEqual(len(data_of(path.read_text())["runs"]), 1)
        self.fixture("queued", plan="second")
        dashboard.regenerate(self.home, heartbeat_seconds=30)
        html = path.read_text()
        self.assertEqual(len(data_of(html)["runs"]), 2)
        self.assertTrue(html.startswith(dashboard.TEMPLATE_HEAD))
        self.assertTrue(html.endswith(dashboard.TEMPLATE_TAIL))
        path.write_text("<html>" + dashboard.DATA_START + "old" + dashboard.DATA_END + "custom renderer</html>")
        dashboard.regenerate(self.home, heartbeat_seconds=30)
        self.assertTrue(path.read_text().endswith(dashboard.TEMPLATE_TAIL))

    def test_lock_contention_skips_without_failing(self):
        import fcntl
        fd = os.open(self.home / "dashboard.lock", os.O_RDWR | os.O_CREAT)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            self.assertIsNone(dashboard.regenerate(self.home, heartbeat_seconds=30, lock_timeout=0.1))
            messages = []
            dashboard.safe_regenerate(self.home, heartbeat_seconds=30, log=messages.append, lock_timeout=0.1)
        finally:
            os.close(fd)
        self.assertEqual(messages, ["dashboard: lock busy, skipped regeneration"])
        self.assertTrue(dashboard.regenerate(self.home, heartbeat_seconds=30).exists())

    def test_render_failure_is_logged_not_raised(self):
        messages = []
        (self.home / "dashboard.html").mkdir()
        dashboard.safe_regenerate(self.home, heartbeat_seconds=30, log=messages.append)
        self.assertTrue(any("render failed" in m for m in messages))

    def test_malformed_run_is_isolated(self):
        self.fixture("queued", plan="good")
        broken = self.fixture("queued", plan="broken")
        (broken.dir / "run.json").write_text("{broken")
        html = dashboard.regenerate(self.home, heartbeat_seconds=30).read_text()
        rows = data_of(html)["runs"]
        self.assertEqual({r["status"] for r in rows}, {"invalid", "queued"})
        with contextlib.redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(cli.main(["ls", "--json"]), 0)
        self.assertEqual(len(json.loads(captured.getvalue())), 2)

    def test_thirty_runs_list_and_render_with_bounded_slots(self):
        for index in range(30):
            self.fixture("queued" if index >= 4 else "running", plan=f"run-{index:02d}")
        held = [slots.try_acquire(self.home / "slots", f"run-{i}", 4) for i in range(4)]
        self.assertIsNone(slots.try_acquire(self.home / "slots", "run-extra", 4))
        rows = dashboard.summaries(self.home, heartbeat_seconds=30)
        self.assertEqual(len(rows), 30)
        html = dashboard.regenerate(self.home, heartbeat_seconds=30).read_text()
        self.assertEqual(len(data_of(html)["runs"]), 30)
        self.assertEqual(len(slots.holders(self.home / "slots")), 4)
        for slot in held:
            slot.release()


class NotifyTests(DashboardTestCase):
    def stub(self, body: str) -> Path:
        path = self.root / "osascript"
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
        return path

    def test_notification_failure_is_non_fatal(self):
        run = self.fixture("needs-human", reason="x")
        os.environ["FACTORY_OSASCRIPT_BIN"] = str(self.stub("echo nope >&2; exit 1\n"))
        try:
            self.assertFalse(notify.send(run, "t", "m", enabled=True))
        finally:
            del os.environ["FACTORY_OSASCRIPT_BIN"]
        names = [e["event"] for e in events.read(run.dir)]
        self.assertIn("notify.failed", names)
        self.assertEqual(Run.load(run.dir).status, "needs-human")

    def test_text_is_passed_as_argv_not_script(self):
        run = self.fixture("done", "ship", pr={"url": "https://github.com/o/r/pull/1"})
        record = self.root / "args"
        os.environ["FACTORY_OSASCRIPT_BIN"] = str(self.stub(f'printf "%s\\n" "$@" > {record}\n'))
        try:
            notify.for_status(run, enabled=True)
        finally:
            del os.environ["FACTORY_OSASCRIPT_BIN"]
        args = record.read_text().splitlines()
        self.assertIn("https://github.com/o/r/pull/1", args)
        self.assertTrue(all('"' not in a for a in args if a.startswith("display")))
        self.assertIn("notify.sent", [e["event"] for e in events.read(run.dir)])

    def test_disabled_sends_nothing(self):
        run = self.fixture("done", "ship")
        self.assertFalse(notify.send(run, "t", "m", enabled=False))
        self.assertNotIn("notify.failed", [e["event"] for e in events.read(run.dir)])


if __name__ == "__main__":
    unittest.main()
