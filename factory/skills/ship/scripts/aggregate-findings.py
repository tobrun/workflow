#!/usr/bin/env python3
"""Number a review panel's findings for verification, then aggregate a complete, verified review record.

    aggregate-findings.py plan <lens-batch>
    aggregate-findings.py pending <batch> --expected name,name
    aggregate-findings.py aggregate <lens-batch> <verifier-batch> --expected lens,lens
                          --revision SHA --diff FILE --out review_N.json [--kind code|spec]
    aggregate-findings.py render <review_N.json>

A batch directory holds `prompts/{task}.md` (what each agent was asked) and
`results/{task}.json` (what it returned; a final message with one fenced JSON block is
accepted). Each result is checked strictly: a lens returns {verdict, findings, good}; a
verifier returns [{id, status, reason}] for exactly the findings it was given.

`plan` prints the BLOCK and CONCERN findings as one JSON array with stable ids (f1, f2, ...).
`pending` prints the tasks that still need a run: no result, an invalid result, or a result
recorded for a different prompt than the current one. Rerun only those, once.
`aggregate` writes the factory.review/1 record. Completeness and verdict are separate:
  - complete requires every expected lens to have a valid result and every BLOCK and
    CONCERN to have a verifier outcome; otherwise the review is incomplete and has no verdict;
  - REFUTED findings drop; a CONFIRMED BLOCK stays a blocker; a PLAUSIBLE BLOCK is kept as an
    explicitly uncertain concern; a missing verifier never demotes anything;
  - verdict is BLOCK with any blocker, CONCERNS with any concern, PASS otherwise.
`render` prints the Markdown verdict sections from a record, so review_N.md and the HTML
report are transcriptions of the same data.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
SEVERITIES = ("BLOCK", "CONCERN", "NIT")
STATUSES = ("CONFIRMED", "PLAUSIBLE", "REFUTED")
SCHEMA = "factory.review/1"
MAX_RESULT_BYTES = 2 * 1024 * 1024


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str | None:
    return sha256_bytes(path.read_bytes()) if path.is_file() else None


def parse_payload(path: Path):
    """A result file's JSON, or a final message wrapping exactly one fenced JSON block; None when neither."""
    if path.stat().st_size > MAX_RESULT_BYTES:
        return None
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    for candidate in [text, *FENCE.findall(text)]:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def lens_problems(payload) -> list[str]:
    if not isinstance(payload, dict):
        return ["is not a JSON object"]
    problems = []
    if payload.get("verdict") not in ("PASS", "CONCERNS", "BLOCK"):
        problems.append("verdict must be PASS, CONCERNS, or BLOCK")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        return problems + ["findings must be a list"]
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            problems.append(f"findings[{index}] is not an object")
            continue
        if finding.get("severity") not in SEVERITIES:
            problems.append(f"findings[{index}].severity must be BLOCK, CONCERN, or NIT")
        for key in ("file", "title", "detail"):
            if not isinstance(finding.get(key), str) or not finding[key].strip():
                problems.append(f"findings[{index}].{key} must be a non-empty string")
        if "line" in finding and not (isinstance(finding["line"], int) and not isinstance(finding["line"], bool)):
            problems.append(f"findings[{index}].line must be an integer")
        if finding.get("severity") == "BLOCK" and not str(finding.get("scenario", "")).strip():
            problems.append(f"findings[{index}] is a BLOCK without a scenario")
    if not isinstance(payload.get("good", []), list):
        problems.append("good must be a list")
    return problems


def batch_tasks(batch: Path) -> dict[str, dict]:
    """Every task of a batch: its prompt hash, and its result when valid."""
    tasks: dict[str, dict] = {}
    for prompt in sorted((batch / "prompts").glob("*.md")) if (batch / "prompts").is_dir() else []:
        tasks.setdefault(prompt.stem, {})["prompt_sha256"] = sha256_file(prompt)
    results = batch / "results"
    for path in sorted(results.iterdir()) if results.is_dir() else []:
        if path.is_file() and path.suffix in (".json", ".out"):
            task = tasks.setdefault(path.stem, {})
            task["result_file"] = path
            task["result_sha256"] = sha256_file(path)
    return tasks


def ledger_path(batch: Path) -> Path:
    return batch / "tasks.json"


def read_ledger(batch: Path) -> dict:
    try:
        return json.loads(ledger_path(batch).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def lens_results(batch: Path) -> tuple[dict[str, dict], dict[str, list[str]]]:
    valid, invalid = {}, {}
    for name, task in batch_tasks(batch).items():
        if "result_file" not in task:
            continue
        payload = parse_payload(task["result_file"])
        problems = ["is not parseable JSON"] if payload is None else lens_problems(payload)
        if problems:
            invalid[name] = problems
        else:
            valid[name] = payload
    return valid, invalid


def collect_findings(results: dict[str, dict]) -> list[dict]:
    findings = []
    for lens, payload in sorted(results.items()):
        for raw in payload["findings"]:
            finding = dict(raw)
            finding["lens"] = lens
            findings.append(finding)
    return findings


def numbered(findings: list[dict]) -> list[dict]:
    out = []
    for finding in findings:
        if finding["severity"] not in ("BLOCK", "CONCERN"):
            continue
        entry = dict(finding)
        entry["id"] = f"f{len(out) + 1}"
        out.append(entry)
    return out


def merge(findings: list[dict]) -> list[dict]:
    """One entry per file:line; keep the fullest detail, credit every lens."""
    merged: dict[tuple, dict] = {}
    for finding in findings:
        key = (finding.get("file"), finding.get("line"))
        existing = merged.get(key)
        if existing is None:
            entry = dict(finding)
            entry["lenses"] = [finding["lens"]]
            entry["ids"] = [finding["id"]] if "id" in finding else []
            entry.pop("lens", None)
            entry.pop("id", None)
            merged[key] = entry
            continue
        if finding["lens"] not in existing["lenses"]:
            existing["lenses"].append(finding["lens"])
        if "id" in finding:
            existing["ids"].append(finding["id"])
        if len(str(finding.get("detail", ""))) > len(str(existing.get("detail", ""))):
            existing["detail"] = finding["detail"]
            existing["title"] = finding.get("title", existing.get("title"))
    return list(merged.values())


def verifier_outcomes(batch: Path, to_verify: dict[str, dict]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    outcomes: dict[str, dict] = {}
    invalid: dict[str, list[str]] = {}
    for name, task in batch_tasks(batch).items():
        if "result_file" not in task:
            continue
        payload = parse_payload(task["result_file"])
        entries = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else None
        problems = []
        if entries is None:
            problems.append("is not parseable JSON")
            entries = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or entry.get("id") not in to_verify:
                problems.append(f"entry {index} names no finding id this panel produced")
                continue
            if entry.get("status") not in STATUSES:
                problems.append(f"{entry['id']}: status must be CONFIRMED, PLAUSIBLE, or REFUTED")
                continue
            if not str(entry.get("reason", "")).strip():
                problems.append(f"{entry['id']}: a verdict needs a reason")
                continue
            if entry["id"] in outcomes and outcomes[entry["id"]]["status"] != entry["status"]:
                problems.append(f"{entry['id']}: conflicting verdicts from more than one verifier")
                continue
            outcomes[entry["id"]] = {"status": entry["status"], "reason": entry["reason"], "verifier": name}
        if problems:
            invalid[name] = problems
    return outcomes, invalid


def cmd_plan(lens_batch: Path) -> int:
    results, invalid = lens_results(lens_batch)
    findings = numbered(collect_findings(results))
    print(json.dumps(findings, indent=2, ensure_ascii=False))
    note = f"{len(findings)} finding(s) to verify from {len(results)} valid lens result(s)"
    if invalid:
        note += "; invalid: " + "; ".join(f"{name} ({', '.join(p[:2])})" for name, p in sorted(invalid.items()))
    print("\n" + note, file=sys.stderr)
    return 0


def cmd_pending(batch: Path, expected: list[str]) -> int:
    """Tasks to (re)run: missing, invalid, or answered for a prompt that has since changed."""
    tasks = batch_tasks(batch)
    ledger = read_ledger(batch)
    pending = []
    names = sorted(set(expected) | set(tasks))
    for name in names:
        task = tasks.get(name, {})
        if "result_file" not in task:
            pending.append({"task": name, "why": "no result"})
            continue
        payload = parse_payload(task["result_file"])
        if payload is None:
            pending.append({"task": name, "why": "unparseable result"})
            continue
        if isinstance(payload, dict) and "findings" in payload and lens_problems(payload):
            pending.append({"task": name, "why": "invalid result: " + "; ".join(lens_problems(payload)[:3])})
            continue
        recorded = ledger.get(name, {}).get("prompt_sha256")
        if recorded is not None and task.get("prompt_sha256") and recorded != task["prompt_sha256"]:
            pending.append({"task": name, "why": "the prompt changed since this result was recorded"})
    print(json.dumps(pending, indent=2))
    return 0


def cmd_aggregate(lens_batch: Path, verifier_batch: Path, expected: list[str], revision: str | None,
                  diff: Path | None, out: Path | None, kind: str) -> int:
    results, invalid_lenses = lens_results(lens_batch)
    findings = collect_findings(results)
    to_verify = {finding["id"]: finding for finding in numbered(findings)}
    outcomes, invalid_verifiers = verifier_outcomes(verifier_batch, to_verify)
    missing_lenses = sorted(set(expected) - set(results))
    unverified = sorted((fid for fid in to_verify if fid not in outcomes), key=lambda f: int(f[1:]))
    complete = bool(expected) and not missing_lenses and not unverified
    blockers, concerns, refuted = [], [], []
    for fid, finding in to_verify.items():
        outcome = outcomes.get(fid)
        entry = {**finding, "verification": outcome["status"] if outcome else "MISSING",
                 "verificationReason": outcome["reason"] if outcome else None}
        if outcome is None:
            (blockers if finding["severity"] == "BLOCK" else concerns).append(entry)
        elif outcome["status"] == "REFUTED":
            refuted.append(entry)
        elif finding["severity"] == "BLOCK" and outcome["status"] == "CONFIRMED":
            blockers.append(entry)
        else:
            entry["reportedSeverity"] = finding["severity"]
            entry["severity"] = "CONCERN"
            concerns.append(entry)
    verdict = None
    if complete:
        verdict = "BLOCK" if blockers else "CONCERNS" if concerns else "PASS"
    ledger = {name: {"prompt_sha256": task.get("prompt_sha256"), "result_sha256": task.get("result_sha256")}
              for batch in (lens_batch, verifier_batch) for name, task in batch_tasks(batch).items()}
    for batch in (lens_batch, verifier_batch):
        ledger_path(batch).parent.mkdir(parents=True, exist_ok=True)
        ledger_path(batch).write_text(json.dumps({k: v for k, v in ledger.items()
                                                  if k in batch_tasks(batch)}, indent=2) + "\n", encoding="utf-8")
    record = {
        "schema": SCHEMA,
        "kind": kind,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "revision": revision,
        "diff_sha256": sha256_file(diff) if diff else None,
        "expected_lenses": sorted(expected),
        "completed_lenses": sorted(results),
        "missing_lenses": missing_lenses,
        "invalid_results": {name: problems for name, problems in sorted({**invalid_lenses, **invalid_verifiers}.items())},
        "verification": {"required": sorted(to_verify, key=lambda f: int(f[1:])),
                         "missing": unverified,
                         "outcomes": {fid: outcomes[fid] for fid in sorted(outcomes, key=lambda f: int(f[1:]))}},
        "completeness": "complete" if complete else "incomplete",
        "verdict": verdict,
        "blockers": merge(blockers),
        "concerns": merge(concerns),
        "refuted": merge(refuted),
        "nits": merge([f for f in findings if f["severity"] == "NIT"]),
        "good": {lens: payload.get("good", []) for lens, payload in sorted(results.items())},
        "tasks": ledger,
    }
    text = json.dumps(record, indent=2, ensure_ascii=False) + "\n"
    if out:
        out.write_text(text, encoding="utf-8")
    print(text, end="")
    if not complete:
        reasons = []
        if not expected:
            reasons.append("no expected lenses were named")
        if missing_lenses:
            reasons.append(f"missing or invalid lenses: {', '.join(missing_lenses)}")
        if unverified:
            reasons.append(f"findings without a verifier outcome: {', '.join(unverified)}")
        print("\nincomplete review: " + "; ".join(reasons) + " - rerun `pending` tasks once", file=sys.stderr)
    return 0


def cmd_render(path: Path) -> int:
    record = json.loads(path.read_text(encoding="utf-8"))
    lines = [f"Verdict: {record['verdict'] or 'INCOMPLETE'}",
             f"Completeness: {record['completeness']}",
             f"Panel: {', '.join(record['completed_lenses']) or 'none'}"
             + (f" (missing: {', '.join(record['missing_lenses'])})" if record["missing_lenses"] else ""),
             ""]
    for title, key in (("Blockers", "blockers"), ("Concerns", "concerns")):
        lines.append(f"## {title}")
        lines.append("")
        for finding in record[key] or []:
            lines.append(f"[{', '.join(finding['lenses'])}] {finding['file']}:{finding.get('line', '?')} - "
                         f"{finding['title']} ({finding['verification']})")
        if not record[key]:
            lines.append("none")
        lines.append("")
    print("\n".join(lines).rstrip())
    return 0


def main(argv: list[str]) -> int:
    args, options = [], {"--expected": "", "--revision": None, "--diff": None, "--out": None, "--kind": "code"}
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value in options and rest:
            options[value] = rest.pop(0)
        else:
            args.append(value)
    expected = [name.strip() for name in (options["--expected"] or "").split(",") if name.strip()]
    if len(args) == 2 and args[0] == "plan":
        return cmd_plan(Path(args[1]))
    if len(args) == 2 and args[0] == "pending":
        return cmd_pending(Path(args[1]), expected)
    if len(args) == 3 and args[0] == "aggregate":
        return cmd_aggregate(Path(args[1]), Path(args[2]), expected, options["--revision"],
                             Path(options["--diff"]) if options["--diff"] else None,
                             Path(options["--out"]) if options["--out"] else None, options["--kind"])
    if len(args) == 2 and args[0] == "render":
        return cmd_render(Path(args[1]))
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
