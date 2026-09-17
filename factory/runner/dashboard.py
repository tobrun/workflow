"""Run summaries shared by the CLI and the static dashboard at ~/.factory/dashboard.html."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from runner import executor, supervise
from runner.model import Run, SchemaError, atomic_write, parse_ts

DATA_START = "<!-- FACTORY_DATA_START -->"
DATA_END = "<!-- FACTORY_DATA_END -->"
ORDER = {"needs-human": 0, "cancelled": 1, "stale": 2, "paused": 2, "running": 3, "queued": 4, "scoping": 5, "new": 5,
         "done": 7, "invalid": 0}


def load_runs(runs_root: Path) -> list[tuple[Path, Run | None, str | None]]:
    found = []
    if not runs_root.is_dir():
        return found
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        if not (run_dir / "run.json").exists():
            continue
        try:
            found.append((run_dir, Run.load(run_dir), None))
        except (SchemaError, OSError, KeyError) as error:
            found.append((run_dir, None, str(error)))
    return found


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(max(0, seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m" if minutes else f"{seconds}s"


def blocker(run: Run) -> str | None:
    reason = run.data["human"].get("reason")
    if reason:
        return reason
    if run.status in ("queued", "running"):
        last = run.last_attempt(run.stage)
        if last and last.get("outcome") and last["outcome"] != "done":
            return f"retrying after: {last.get('reason')}"
    return None


def last_decision(run: Run) -> str | None:
    decisions = run.data.get("decisions") or []
    if not decisions:
        return None
    last = decisions[-1]
    if last.get("source") != "foreman":
        return f"foreman fell back: {last.get('reason')}"
    return f"foreman {last.get('action')}: {last.get('summary')}"


def next_command(run: Run, alive: bool, stale: bool) -> str | None:
    rid = run.id
    if run.status == "scoping":
        return f"factory scope {rid} --resume"
    if run.status == "new":
        return f"factory rm {rid} --force"
    if stale:
        return f"factory resume {rid}"
    if run.status in ("queued", "running"):
        return f"factory watch {rid}"
    if run.status == "paused":
        return f"factory resume {rid}"
    if run.status == "needs-human":
        return run.data["human"].get("operator_action") or f'factory retry {rid} --note "..."'
    if run.status == "cancelled":
        exhausted = run.budget_remaining() <= 0 and run.stage in ("scope-review", "build", "ship")
        return f"factory retry {rid}" + (" --reset-budget" if exhausted else "")
    if run.status == "done":
        return "factory gc" if not run.data.get("archived") else None
    return None


def summarize(run_dir: Path, run: Run | None, error: str | None, *, heartbeat_seconds: float,
              now: float | None = None) -> dict:
    now = now or time.time()
    if run is None:
        return {"id": run_dir.name, "status": "invalid", "group": "needs", "order": ORDER["invalid"],
                "blocker": error, "repo": None, "stage": None, "retries": None, "pr": None, "updated_at": None,
                "duration": None, "worker_alive": False, "stale": False, "next": None}
    alive = supervise.worker_alive(run.dir)
    age = supervise.heartbeat_age(run.dir)
    stale = run.status in ("queued", "running") and (not alive or (age is not None and age > 3 * heartbeat_seconds))
    execution_alive: bool | None = False
    if run.status == "running" and not alive:
        try:
            execution_alive = executor.live(run.dir)
        except executor.Ambiguous:
            execution_alive = None
    created = parse_ts(run.data["created_at"])
    updated = parse_ts(run.data["updated_at"])
    end = updated.timestamp() if run.status == "done" and updated else now
    status_key = "stale" if stale else run.status
    group = "needs" if run.status in ("needs-human", "cancelled", "paused") or stale else ("done" if run.status == "done" else "flight")
    return {
        "id": run.id,
        "repo": run.data["repo"],
        "branch": run.data["branch"],
        "stage": run.stage,
        "status": run.status,
        "group": group,
        "order": ORDER.get(status_key, 6),
        "retries": f"{run.data['retries']['used']}/{run.data['retries']['budget']}",
        "blocker": blocker(run),
        "note": run.data["human"].get("note"),
        "decision": last_decision(run),
        "overrides": len(run.data.get("overrides") or []),
        "pr": run.data["pr"] if run.data["pr"].get("url") else None,
        "updated_at": run.data["updated_at"],
        "created_at": run.data["created_at"],
        "duration": human_duration(end - created.timestamp()) if created else None,
        "tokens": run.data.get("tokens_total", 0),
        "cached_input_tokens": (run.data.get("usage") or {}).get("cached_input"),
        "queue_seconds": sum(a.get("queue_seconds") or 0 for a in run.data["attempts"]),
        "gate_seconds": round(sum(a.get("gate_seconds") or 0 for a in run.data["attempts"]), 1),
        "worker_alive": alive,
        "heartbeat_age": None if age is None else int(age),
        "stale": stale,
        "execution_alive": execution_alive,
        "archived": bool(run.data.get("archived")),
        "next": next_command(run, alive, stale),
    }


def summaries(home: Path, *, heartbeat_seconds: float, include_archive: bool = False) -> list[dict]:
    rows = [summarize(d, r, e, heartbeat_seconds=heartbeat_seconds) for d, r, e in load_runs(home / "runs")]
    if include_archive:
        for run_dir, run, error in load_runs(home / "archive"):
            row = summarize(run_dir, run, error, heartbeat_seconds=heartbeat_seconds)
            row["archived"] = True
            row["next"] = None
            rows.append(row)
    return sort_rows(rows)


def sort_rows(rows: list[dict]) -> list[dict]:
    active = sorted((r for r in rows if r["group"] != "done"), key=lambda r: (r["order"], r.get("updated_at") or ""))
    done = sorted((r for r in rows if r["group"] == "done"), key=lambda r: r.get("updated_at") or "", reverse=True)
    return active + done


# --- static dashboard ------------------------------------------------------------

def data_block(rows: list[dict]) -> str:
    payload = json.dumps({"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "runs": rows},
                         ensure_ascii=False, indent=1)
    # Neutralize markup so run text can never close the script element.
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f'{DATA_START}\n<script id="factory-data" type="application/json">{payload}</script>\n{DATA_END}'


TEMPLATE_HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Factory</title>
<style>
:root {
  --bg: #f7f6f3; --panel: #ffffff; --ink: #1d1d1b; --muted: #6b6a66; --line: #e4e2dc;
  --needs: #b3261e; --needs-bg: #fbeae8; --flight: #1f5fbf; --flight-bg: #e8f0fb; --done: #2e7d32; --done-bg: #e8f3e9;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #141413; --panel: #1d1d1b; --ink: #ecebe6; --muted: #9c9a94; --line: #2f2e2b;
    --needs: #f2b8b5; --needs-bg: #3a1f1d; --flight: #a8c7fa; --flight-bg: #1b2a40; --done: #a5d6a7; --done-bg: #1d3320;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink); font: 14px/1.45 ui-sans-serif, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1480px; margin: 0 auto; padding: 32px 16px 64px; }
header { display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 8px; margin-bottom: 24px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
.meta { color: var(--muted); font-size: 13px; }
.counts { display: flex; gap: 8px; flex-wrap: wrap; }
.pill { border-radius: 999px; padding: 2px 10px; font-size: 12px; font-weight: 600; }
section { margin-top: 28px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); margin: 0 0 10px; }
.scroll { overflow-x: auto; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; min-width: 1100px; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { font-size: 12px; font-weight: 600; color: var(--muted); white-space: nowrap; }
tr:last-child td { border-bottom: 0; }
td.id, td.nowrap { white-space: nowrap; }
td.id { font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12.5px; white-space: nowrap; }
td.blocker { overflow-wrap: anywhere; }
td.id { overflow: hidden; text-overflow: ellipsis; }
.next { display: block; margin-top: 4px; font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; color: var(--muted); }
.status { white-space: nowrap; }
.needs { color: var(--needs); background: var(--needs-bg); }
.flight { color: var(--flight); background: var(--flight-bg); }
.done { color: var(--done); background: var(--done-bg); }
.empty { padding: 14px 12px; color: var(--muted); }
a { color: inherit; }
</style>
</head>
<body>
<main>
<header><h1>Factory</h1><div class="meta" id="meta"></div></header>
<div class="counts" id="counts"></div>
<div id="groups"></div>
</main>
"""

TEMPLATE_TAIL = """
<script>
(function () {
  var data = JSON.parse(document.getElementById("factory-data").textContent);
  var groups = [["needs", "Needs you"], ["flight", "In flight"], ["done", "Done"]];
  var columns = ["Run", "Repository", "Stage", "Status", "Retries", "Blocker", "Pull request", "Updated", "Duration"];
  var widths = ["250px", "130px", "104px", "176px", "60px", "auto", "90px", "140px", "72px"];
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }
  function when(ts) {
    if (!ts) return "-";
    var d = new Date(ts);
    return isNaN(d) ? ts : d.toLocaleString(undefined, {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
  }
  document.getElementById("meta").textContent = "Generated " + when(data.generated_at) + " - refreshes every 30s";
  var counts = document.getElementById("counts");
  var root = document.getElementById("groups");
  groups.forEach(function (group) {
    var rows = data.runs.filter(function (r) { return r.group === group[0]; });
    counts.appendChild(el("span", "pill " + group[0], group[1] + " " + rows.length));
    var section = el("section");
    section.appendChild(el("h2", null, group[1]));
    var scroll = el("div", "scroll");
    if (!rows.length) {
      scroll.appendChild(el("div", "empty", "Nothing here."));
    } else {
      var table = el("table");
      var colgroup = el("colgroup");
      widths.forEach(function (w) { var col = el("col"); col.style.width = w; colgroup.appendChild(col); });
      table.appendChild(colgroup);
      var head = el("tr");
      columns.forEach(function (c) { head.appendChild(el("th", null, c)); });
      table.appendChild(el("thead")).appendChild(head);
      var body = el("tbody");
      rows.forEach(function (r) {
        var tr = el("tr");
        tr.appendChild(el("td", "id", r.id)).title = r.id;
        tr.appendChild(el("td", "nowrap", r.repo ? r.repo.split("/").slice(-1)[0] : "-")).title = r.repo || "";
        tr.appendChild(el("td", "nowrap", r.stage || "-"));
        var status = el("td", "status");
        status.appendChild(el("span", "pill " + group[0], r.stale ? r.status + (r.execution_alive ? " (worker down, execution alive)" : " (worker down)") : r.status));
        tr.appendChild(status);
        tr.appendChild(el("td", "nowrap", r.retries || "-"));
        var blocker = el("td", "blocker", r.blocker || r.decision || r.note || "-");
        if (r.next && group[0] === "needs") blocker.appendChild(el("span", "next", r.next));
        tr.appendChild(blocker);
        var pr = el("td", "nowrap");
        if (r.pr && r.pr.url && /^https?:\\/\\//.test(r.pr.url)) {
          var link = el("a", null, "#" + r.pr.number + (r.pr.draft ? " draft" : ""));
          link.href = r.pr.url;
          pr.appendChild(link);
        } else {
          pr.textContent = "-";
        }
        tr.appendChild(pr);
        tr.appendChild(el("td", "nowrap", when(r.updated_at)));
        tr.appendChild(el("td", "nowrap", r.duration || "-"));
        body.appendChild(tr);
      });
      table.appendChild(body);
      scroll.appendChild(table);
    }
    section.appendChild(scroll);
    root.appendChild(section);
  });
})();
</script>
</body>
</html>
"""


def render(rows: list[dict], existing: str | None = None) -> str:
    block = data_block(rows)
    if existing and DATA_START in existing and DATA_END in existing:
        head, _, rest = existing.partition(DATA_START)
        _, _, tail = rest.partition(DATA_END)
        if head == TEMPLATE_HEAD and tail == TEMPLATE_TAIL:
            return head + block + tail
    return TEMPLATE_HEAD + block + TEMPLATE_TAIL


def regenerate(home: Path, *, heartbeat_seconds: float, lock_timeout: float = 10) -> Path | None:
    """Rewrite the dashboard under dashboard.lock. Returns None when the lock stayed busy."""
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "dashboard.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + lock_timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return None
                time.sleep(0.05)
        path = home / "dashboard.html"
        existing = path.read_text(encoding="utf-8") if path.exists() else None
        rows = summaries(home, heartbeat_seconds=heartbeat_seconds, include_archive=True)
        atomic_write(path, render(rows, existing))
        return path
    finally:
        os.close(fd)


def safe_regenerate(home: Path, *, heartbeat_seconds: float, log=None, lock_timeout: float = 10) -> None:
    """Dashboard failures are logged and never fail the run."""
    try:
        if regenerate(home, heartbeat_seconds=heartbeat_seconds, lock_timeout=lock_timeout) is None and log:
            log("dashboard: lock busy, skipped regeneration")
    except Exception as error:  # noqa: BLE001 - rendering must never break a transition
        if log:
            log(f"dashboard: render failed: {error}")
