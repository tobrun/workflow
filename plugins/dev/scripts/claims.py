#!/usr/bin/env python3
"""The dev workflow's memory of its own runs: cited claims about the skills.

Usage:
  python3 claims.py pending                      journal runs not yet consolidated, as JSON
  python3 claims.py add CANDIDATES.jsonl         validate and append claims, all or nothing
  python3 claims.py mark RUN_ID ...              record runs as consolidated
  python3 claims.py rebuild                      regenerate every derived file from the records
  python3 claims.py check                        validate the records and that derived files are current
  python3 claims.py top [--limit N] [--all]      rank threads, open and reopened first
  python3 claims.py show TARGET                  a thread, skill, or claim with its evidence
  python3 claims.py retract CLAIM_ID --reason R  forget a claim and rebuild without it
  python3 claims.py resolve THREAD --commit SHA [--eval NAME]
  python3 claims.py report OUT.html              render the threads as a self-contained page

The store lives in $DEV_MEMORY_DIR (default ~/.dev-workflow/memory). The records
are journal/*.jsonl (written by skill-metrics.py end), claims.jsonl,
resolutions.jsonl, and consolidated.jsonl; skills/*.md, threads.yaml, and
progression.md are derived from them by `rebuild` and never edited by hand.

A claim is accepted only when its quote is found verbatim (whitespace aside) at
its cited source: a transcript line of the run it names, or that run's journal
entry. A retracted claim stays in claims.jsonl so the same evidence cannot
bring it back. A thread resolved by a commit reopens when a claim observed
after the resolution joins it. Nothing here edits a skill.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

KINDS = ("friction", "defect", "cost", "success")
SLUG = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
CLAIM_ID = re.compile(r"^c-\d{4,}$")
FIELDS = {"skill", "thread", "kind", "claim", "quote", "source", "salience", "supersedes"}
CREDENTIAL = re.compile(
    r"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_\w{20,}|sk-[A-Za-z0-9_-]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,})"
)
EM_DASH = "\u2014"
DERIVED_NOTE = "Derived by claims.py rebuild from the records; never edit by hand."


# --- store -------------------------------------------------------------------

def root() -> Path:
    value = os.environ.get("DEV_MEMORY_DIR", "~/.dev-workflow/memory")
    if value.strip().lower() in ("off", "0", "false", ""):
        sys.exit("claims: the memory store is turned off (DEV_MEMORY_DIR=off)")
    return Path(value).expanduser()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def runs(store: Path) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in sorted((store / "journal").glob("*.jsonl")):
        for row in read_jsonl(path):
            if row.get("id"):
                found[row["id"]] = row
    return found


def claims(store: Path) -> list[dict]:
    return read_jsonl(store / "claims.jsonl")


def norm(text: str) -> str:
    return " ".join(str(text).split())


def strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.values() for s in strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in strings(item)]
    return []


def transcript_line(path: Path, line: int) -> dict | None:
    try:
        with path.open(encoding="utf-8") as handle:
            for index, raw in enumerate(handle):
                if index == line:
                    return json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None
    return None


# --- validation --------------------------------------------------------------

def source_text(source: dict, run: dict) -> tuple[str | None, str]:
    """The text a claim's quote must appear in, and a label for messages."""
    if "line" not in source:
        return "\n".join(strings(run)), f"journal entry {run['id']}"
    if not isinstance(source["line"], int) or source["line"] < 0:
        return None, "source.line must be a non-negative integer"
    main = Path(run.get("transcript") or "")
    target = Path(source.get("transcript") or main)
    allowed = target == main or main.with_suffix("") in target.parents
    if not run.get("transcript") or not allowed:
        return None, f"source.transcript must be the run's transcript or one of its subagents ({main})"
    obj = transcript_line(target, source["line"])
    if obj is None:
        return None, f"{target} has no readable line {source['line']}"
    return "\n".join(strings(obj.get("message", obj))), f"{target.name}:{source['line']}"


def evidence_key(row: dict) -> tuple:
    source = row.get("source") or {}
    return (source.get("run"), source.get("transcript"), source.get("line"), norm(row.get("quote", "")).lower())


def shape_errors(row: dict) -> list[str]:
    errors = []
    extra = set(row) - FIELDS - {"id", "status", "observed_at", "repo", "added_at", "reason"}
    if extra:
        errors.append(f"unknown fields {sorted(extra)}")
    for key in ("skill", "thread"):
        if not SLUG.match(str(row.get(key, ""))):
            errors.append(f"{key} must be a lowercase slug, got {row.get(key)!r}")
    if row.get("kind") not in KINDS:
        errors.append(f"kind must be one of {', '.join(KINDS)}")
    claim = str(row.get("claim", ""))
    if not 12 <= len(claim) <= 240 or "\n" in claim:
        errors.append("claim must be one line of 12 to 240 characters")
    if EM_DASH in claim:
        errors.append("claim contains an em dash; use two plain dashes")
    quote = norm(row.get("quote", ""))
    if not 8 <= len(quote) <= 300:
        errors.append("quote must be 8 to 300 characters of verbatim evidence")
    if row.get("salience") not in (1, 2, 3):
        errors.append("salience must be 1, 2, or 3")
    if not isinstance(row.get("source"), dict) or not row["source"].get("run"):
        errors.append("source must be an object naming its run")
    if CREDENTIAL.search(claim) or CREDENTIAL.search(quote):
        errors.append("claim or quote matches a credential shape; quote around the secret")
    return errors


def validate_candidates(rows: list[dict], store: Path) -> list[str]:
    known = runs(store)
    existing = claims(store)
    by_id = {c["id"]: c for c in existing}
    seen = {evidence_key(c): c for c in existing}
    errors: list[str] = []
    batch: set[tuple] = set()
    for number, row in enumerate(rows, 1):
        problems = shape_errors(row)
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        run = known.get(source.get("run", ""))
        if source.get("run") and run is None:
            problems.append(f"source.run {source['run']!r} is not in the journal")
        if run is not None and not problems:
            text, label = source_text(source, run)
            if text is None:
                problems.append(label)
            elif norm(row["quote"]).lower() not in norm(text).lower():
                problems.append(f"quote not found verbatim in {label}")
        key = evidence_key(row)
        if key in seen:
            prior = seen[key]
            verb = "was retracted as" if prior.get("status") == "retracted" else "duplicates"
            problems.append(f"this evidence {verb} {prior['id']}")
        if key in batch:
            problems.append("duplicates an earlier candidate in this file")
        batch.add(key)
        target = row.get("supersedes")
        if target is not None and (target not in by_id or by_id[target].get("status") != "active"):
            problems.append(f"supersedes {target!r}, which is not an active claim")
        errors += [f"candidate {number}: {p}" for p in problems]
    return errors


# --- derived state -----------------------------------------------------------

def threads(store: Path) -> list[dict]:
    resolutions: dict[str, dict] = {}
    for row in read_jsonl(store / "resolutions.jsonl"):
        resolutions[row["thread"]] = row
    grouped: dict[str, list[dict]] = {}
    for claim in claims(store):
        if claim.get("status") == "active":
            grouped.setdefault(claim["thread"], []).append(claim)
    for thread in resolutions:
        grouped.setdefault(thread, [])
    out = []
    for name, rows in grouped.items():
        resolved = resolutions.get(name)
        problems = [c for c in rows if c["kind"] != "success"]
        since = [c for c in problems if resolved and c["observed_at"][:19] > resolved["at"][:19]]
        if not resolved:
            status = "open" if problems else "working"
        else:
            status = "reopened" if since else "resolved"
        live = since if resolved else problems
        out.append({
            "thread": name,
            "skills": sorted({c["skill"] for c in rows}) or [resolved.get("skill", "?")],
            "status": status,
            "score": sum(c["salience"] for c in live),
            "runs": len({c["source"]["run"] for c in live}),
            "repos": len({c.get("repo") for c in live}),
            "last_seen": max((c["observed_at"] for c in rows), default=""),
            "claims": [c["id"] for c in rows],
            "resolution": resolved,
            "claims_before": len(problems) - len(since),
            "claims_since": len(since),
        })
    order = {"reopened": 0, "open": 1, "working": 2, "resolved": 3}
    out.sort(key=lambda t: (order[t["status"]], -t["runs"], -t["score"], t["thread"]))
    return out


def yaml_scalar(value) -> str:
    text = str(value)
    return text if re.match(r"^[A-Za-z0-9_./:@+-]+$", text) else json.dumps(text)


def render_threads(rows: list[dict]) -> str:
    lines = [f"# {DERIVED_NOTE}"]
    for t in rows:
        lines.append(f"- thread: {t['thread']}")
        for key in ("status", "score", "runs", "repos", "last_seen"):
            lines.append(f"  {key}: {yaml_scalar(t[key])}")
        lines.append(f"  skills: [{', '.join(t['skills'])}]")
        lines.append(f"  claims: [{', '.join(t['claims'])}]")
        if t["resolution"]:
            r = t["resolution"]
            lines.append(f"  resolved: {{commit: {yaml_scalar(r['commit'])}, at: {yaml_scalar(r['at'])}"
                         + (f", eval: {yaml_scalar(r['eval'])}" if r.get("eval") else "") + "}")
    return "\n".join(lines) + "\n"


def render_skill(skill: str, rows: list[dict], by_id: dict[str, dict]) -> str:
    out = [f"# {skill}", "", DERIVED_NOTE, ""]
    sections = (("Open threads", ("reopened", "open")), ("What works", ("working",)), ("Resolved threads", ("resolved",)))
    for title, statuses in sections:
        picked = [t for t in rows if t["status"] in statuses and skill in t["skills"]]
        if not picked:
            continue
        out += [f"## {title}", ""]
        for t in picked:
            out.append(f"### {t['thread']} ({t['status']}, score {t['score']}, {t['runs']} runs, {t['repos']} repos)")
            out.append("")
            for cid in t["claims"]:
                c = by_id[cid]
                if c["skill"] == skill:
                    out.append(f"- [{cid}] {c['claim']} ({c['kind']}, salience {c['salience']}, "
                               f"{c.get('repo', '?')}, {c['observed_at'][:10]})")
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def render_progression(rows: list[dict]) -> str:
    out = ["# Progression", "", DERIVED_NOTE, "",
           "| thread | resolved at | commit | eval | claims before | claims since | status |",
           "| ------ | ----------- | ------ | ---- | ------------- | ------------ | ------ |"]
    for t in sorted((t for t in rows if t["resolution"]), key=lambda t: t["resolution"]["at"]):
        r = t["resolution"]
        out.append(f"| {t['thread']} | {r['at'][:10]} | {r['commit'][:12]} | {r.get('eval') or '-'} | "
                   f"{t['claims_before']} | {t['claims_since']} | {t['status']} |")
    return "\n".join(out) + "\n"


def derived(store: Path) -> dict[str, str]:
    rows = threads(store)
    by_id = {c["id"]: c for c in claims(store)}
    files = {"threads.yaml": render_threads(rows), "progression.md": render_progression(rows)}
    for skill in sorted({s for t in rows for s in t["skills"] if t["claims"]}):
        files[f"skills/{skill}.md"] = render_skill(skill, rows, by_id)
    return files


def rebuild(store: Path) -> None:
    skills_dir = store / "skills"
    if skills_dir.is_dir():
        for stale in skills_dir.glob("*.md"):
            stale.unlink()
    for name, text in derived(store).items():
        path = store / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# --- commands ----------------------------------------------------------------

def cmd_pending(store: Path) -> int:
    done = {row["run"] for row in read_jsonl(store / "consolidated.jsonl")}
    todo = [run for rid, run in sorted(runs(store).items()) if rid not in done]
    print(json.dumps(todo, indent=1))
    print(f"claims: {len(todo)} pending runs", file=sys.stderr)
    return 0


def cmd_add(store: Path, candidates: Path) -> int:
    rows = []
    for number, line in enumerate(candidates.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"{candidates}:{number}: not JSON ({exc.msg})")
            return 1
    errors = validate_candidates(rows, store)
    if errors:
        print("\n".join(errors))
        print(f"claims: {len(errors)} problems, nothing added; fix the candidates, not the evidence")
        return 1
    existing = claims(store)
    known = runs(store)
    next_id = max((int(c["id"][2:]) for c in existing), default=0) + 1
    superseded = {row["supersedes"] for row in rows if row.get("supersedes")}
    for claim in existing:
        if claim["id"] in superseded:
            claim["status"] = "superseded"
    added = []
    for row in rows:
        run = known[row["source"]["run"]]
        row.update({"id": f"c-{next_id:04d}", "status": "active", "observed_at": run["started_at"],
                    "repo": run.get("repo"), "added_at": now_iso(), "quote": norm(row["quote"])})
        next_id += 1
        added.append(row)
    write_jsonl(store / "claims.jsonl", existing + added)
    rebuild(store)
    print(f"claims: added {', '.join(r['id'] for r in added) or 'nothing'}"
          + (f"; superseded {', '.join(sorted(superseded))}" if superseded else ""))
    return 0


def cmd_mark(store: Path, ids: list[str]) -> int:
    known = runs(store)
    missing = [i for i in ids if i not in known]
    if missing:
        print(f"claims: not in the journal: {', '.join(missing)}")
        return 1
    done = {row["run"] for row in read_jsonl(store / "consolidated.jsonl")}
    append_jsonl(store / "consolidated.jsonl", [{"run": i, "at": now_iso()} for i in ids if i not in done])
    print(f"claims: {len(ids)} runs marked consolidated")
    return 0


def cmd_check(store: Path) -> int:
    rows = claims(store)
    errors = []
    ids = [r.get("id") for r in rows]
    if len(ids) != len(set(ids)):
        errors.append("claims.jsonl: duplicate claim ids")
    for row in rows:
        label = row.get("id", "?")
        if not CLAIM_ID.match(str(label)):
            errors.append(f"{label}: malformed id")
        if row.get("status") not in ("active", "superseded", "retracted"):
            errors.append(f"{label}: unknown status {row.get('status')!r}")
        errors += [f"{label}: {p}" for p in shape_errors(row)]
        if row.get("supersedes") and row["supersedes"] not in ids:
            errors.append(f"{label}: supersedes unknown {row['supersedes']}")
    for name, text in derived(store).items():
        path = store / name
        if not path.exists() or path.read_text(encoding="utf-8") != text:
            errors.append(f"{name}: stale; run `claims.py rebuild`")
    print("\n".join(errors) if errors else f"claims: {len(rows)} claims, derived files current")
    return 1 if errors else 0


def cmd_top(store: Path, limit: int, everything: bool) -> int:
    rows = [t for t in threads(store) if everything or t["status"] in ("open", "reopened")]
    if not rows:
        print("claims: no open threads")
        return 0
    print("| thread | skills | status | score | runs | repos | last seen |")
    print("| ------ | ------ | ------ | ----- | ---- | ----- | --------- |")
    for t in rows[:limit]:
        print(f"| {t['thread']} | {', '.join(t['skills'])} | {t['status']} | {t['score']} | "
              f"{t['runs']} | {t['repos']} | {t['last_seen'][:10]} |")
    return 0


def cmd_show(store: Path, target: str) -> int:
    rows = claims(store)
    picked = [c for c in rows if target in (c["id"], c["thread"], c["skill"])]
    if not picked:
        print(f"claims: nothing matches {target!r}")
        return 1
    known = runs(store)
    for c in picked:
        source = c["source"]
        print(f"## {c['id']} [{c['status']}] {c['thread']} ({c['skill']}, {c['kind']}, salience {c['salience']})")
        print(f"claim: {c['claim']}")
        print(f"quote: \"{c['quote']}\"")
        run = known.get(source["run"])
        where = f"{source['run']}" + (f" line {source['line']}" if "line" in source else " journal entry")
        print(f"source: {where} ({c.get('repo', '?')}, {c['observed_at']})")
        if run is not None and "line" in source:
            text, label = source_text(source, run)
            if text is not None:
                flat = norm(text)
                at = flat.lower().find(c["quote"].lower())
                start = max(0, at - 200) if at >= 0 else 0
                print(f"context ({label}): ...{flat[start:start + len(c['quote']) + 400]}...")
            else:
                print(f"context: unavailable ({label})")
        if c.get("reason"):
            print(f"retracted because: {c['reason']}")
        print()
    return 0


def cmd_retract(store: Path, claim_id: str, reason: str) -> int:
    rows = claims(store)
    hit = next((c for c in rows if c["id"] == claim_id), None)
    if hit is None or hit["status"] == "retracted":
        print(f"claims: {claim_id} is not a claim that can be retracted")
        return 1
    hit["status"] = "retracted"
    hit["reason"] = " ".join(reason.split())
    write_jsonl(store / "claims.jsonl", rows)
    rebuild(store)
    print(f"claims: retracted {claim_id}; derived files rebuilt without it")
    return 0


def cmd_resolve(store: Path, thread: str, commit: str, eval_name: str | None) -> int:
    if not any(c["thread"] == thread and c["status"] == "active" for c in claims(store)):
        print(f"claims: no active claims in thread {thread!r}")
        return 1
    if not re.match(r"^[0-9a-f]{7,40}$", commit):
        print("claims: --commit must be a commit sha")
        return 1
    skill = next(c["skill"] for c in claims(store) if c["thread"] == thread)
    append_jsonl(store / "resolutions.jsonl",
                 [{"thread": thread, "skill": skill, "commit": commit, "eval": eval_name, "at": now_iso()}])
    rebuild(store)
    print(f"claims: {thread} resolved by {commit}; a later claim in it reopens it")
    return 0


def cmd_report(store: Path, out: Path) -> int:
    rows = threads(store)
    by_id = {c["id"]: c for c in claims(store)}
    cards = []
    for t in rows:
        items = "".join(
            f"<li><b>{html.escape(cid)}</b> {html.escape(by_id[cid]['claim'])}"
            f"<blockquote>{html.escape(by_id[cid]['quote'])}</blockquote>"
            f"<small>{html.escape(by_id[cid]['kind'])} / salience {by_id[cid]['salience']} / "
            f"{html.escape(str(by_id[cid].get('repo')))} / {html.escape(by_id[cid]['source']['run'])}</small></li>"
            for cid in t["claims"])
        resolved = (f"<p>resolved by <code>{html.escape(t['resolution']['commit'][:12])}</code></p>"
                    if t["resolution"] else "")
        cards.append(
            f"<details{' open' if t['status'] in ('open', 'reopened') else ''}><summary>"
            f"<span class=s-{t['status']}>{t['status']}</span> <b>{html.escape(t['thread'])}</b> "
            f"<small>{html.escape(', '.join(t['skills']))} / score {t['score']} / {t['runs']} runs / "
            f"{t['repos']} repos</small></summary>{resolved}<ul>{items}</ul></details>")
    page = f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Workflow Memory</title>
<style>:root{{--bg:#fbfaf7;--fg:#1d1d1b;--mute:#6b6a65;--line:#e4e1d8;--open:#b5462f;--ok:#2f7a4f;--re:#9a5b00}}
@media (prefers-color-scheme:dark){{:root{{--bg:#191917;--fg:#eceae4;--mute:#a19f98;--line:#33322e;--open:#e57a62;--ok:#6cc08f;--re:#e0a64a}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:0 auto;padding:24px 16px}}
details{{border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:10px 0}}summary{{cursor:pointer}}
small{{color:var(--mute)}}blockquote{{margin:6px 0;padding-left:10px;border-left:3px solid var(--line);color:var(--mute)}}
.s-open{{color:var(--open)}}.s-reopened{{color:var(--re)}}.s-resolved,.s-working{{color:var(--ok)}}li{{margin:8px 0}}</style>
</head><body><h1>Workflow memory</h1><p><small>{len(rows)} threads from {len(by_id)} claims, rendered {now_iso()}.
Every claim quotes the run it came from.</small></p>{''.join(cards) or '<p>No claims yet.</p>'}</body></html>
"""
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"claims: report written to {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pending")
    sub.add_parser("add").add_argument("candidates", type=Path)
    sub.add_parser("mark").add_argument("runs", nargs="+")
    sub.add_parser("rebuild")
    sub.add_parser("check")
    top = sub.add_parser("top")
    top.add_argument("--limit", type=int, default=10)
    top.add_argument("--all", action="store_true")
    sub.add_parser("show").add_argument("target")
    retract = sub.add_parser("retract")
    retract.add_argument("claim")
    retract.add_argument("--reason", required=True)
    resolve = sub.add_parser("resolve")
    resolve.add_argument("thread")
    resolve.add_argument("--commit", required=True)
    resolve.add_argument("--eval", dest="eval_name")
    sub.add_parser("report").add_argument("out", type=Path)
    args = parser.parse_args()
    store = root()
    if args.command == "pending":
        return cmd_pending(store)
    if args.command == "add":
        return cmd_add(store, args.candidates)
    if args.command == "mark":
        return cmd_mark(store, args.runs)
    if args.command == "rebuild":
        rebuild(store)
        print(f"claims: derived files rebuilt under {store}")
        return 0
    if args.command == "check":
        return cmd_check(store)
    if args.command == "top":
        return cmd_top(store, args.limit, args.all)
    if args.command == "show":
        return cmd_show(store, args.target)
    if args.command == "retract":
        return cmd_retract(store, args.claim, args.reason)
    if args.command == "resolve":
        return cmd_resolve(store, args.thread, args.commit, args.eval_name)
    return cmd_report(store, args.out)


if __name__ == "__main__":
    sys.exit(main())
