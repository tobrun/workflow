#!/usr/bin/env python3
"""Number the panel's findings for verification, then apply the verdicts.

    aggregate-findings.py plan <lens-results-dir>
    aggregate-findings.py aggregate <lens-results-dir> <verifier-results-dir>
                          [--expected lens,lens]

`plan` prints the BLOCK and CONCERN findings as one JSON array with stable ids
(f1, f2, ...) to hand to the verifiers. `aggregate` applies their verdicts and
prints the report data: refuted findings dropped, an unconfirmed BLOCK demoted
to CONCERN, duplicates merged by file:line, and the overall verdict.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
RESULT_SUFFIXES = (".json", ".out")


def load_json(path: Path):
    """Result files are JSON, or a final message wrapping one fenced block."""
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


def read_results(directory: Path) -> tuple[dict[str, object], list[str]]:
    """Every parseable result file by agent name, plus the names that failed."""
    parsed: dict[str, object] = {}
    unreadable: list[str] = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix not in RESULT_SUFFIXES:
            continue
        payload = load_json(path)
        if payload is None:
            unreadable.append(path.stem)
        else:
            parsed[path.stem] = payload
    return parsed, unreadable


def collect_findings(lens_results: dict[str, object]) -> list[dict]:
    findings = []
    for lens, payload in lens_results.items():
        if not isinstance(payload, dict):
            continue
        for raw in payload.get("findings", []) or []:
            finding = dict(raw)
            finding["lens"] = lens
            findings.append(finding)
    return findings


def numbered(findings: list[dict]) -> list[dict]:
    """BLOCK and CONCERN findings get an id; NITs skip verification."""
    out = []
    for finding in findings:
        if str(finding.get("severity", "")).upper() not in ("BLOCK", "CONCERN"):
            continue
        entry = dict(finding)
        entry["id"] = f"f{len(out) + 1}"
        out.append(entry)
    return out


def merge(findings: list[dict]) -> list[dict]:
    """One entry per file:line; keep the fullest detail, credit every lens."""
    merged: dict[tuple, dict] = {}
    for finding in findings:
        key = (finding.get("file"), finding.get("line"), )
        existing = merged.get(key)
        if existing is None:
            entry = dict(finding)
            entry["lenses"] = [finding.get("lens")] if finding.get("lens") else []
            entry.pop("lens", None)
            merged[key] = entry
            continue
        if finding.get("lens") and finding["lens"] not in existing["lenses"]:
            existing["lenses"].append(finding["lens"])
        if len(str(finding.get("detail", ""))) > len(str(existing.get("detail", ""))):
            existing["detail"] = finding["detail"]
            existing["title"] = finding.get("title", existing.get("title"))
    return list(merged.values())


def cmd_plan(lens_dir: Path) -> int:
    lens_results, unreadable = read_results(lens_dir)
    findings = numbered(collect_findings(lens_results))
    print(json.dumps(findings, indent=2, ensure_ascii=False))
    print(
        f"\n{len(findings)} finding(s) to verify from {len(lens_results)} lens result(s)"
        + (f"; unparseable: {', '.join(unreadable)}" if unreadable else ""),
        file=sys.stderr,
    )
    return 0


def cmd_aggregate(lens_dir: Path, verifier_dir: Path, expected: list[str]) -> int:
    lens_results, unreadable = read_results(lens_dir)
    all_findings = collect_findings(lens_results)
    to_verify = numbered(all_findings)

    verdicts: dict[str, dict] = {}
    verifier_results, verifier_unreadable = read_results(verifier_dir)
    for payload in verifier_results.values():
        for entry in payload if isinstance(payload, list) else [payload]:
            if isinstance(entry, dict) and entry.get("id"):
                verdicts[entry["id"]] = entry

    blockers, concerns = [], []
    for finding in to_verify:
        verdict = verdicts.get(finding["id"], {})
        status = str(verdict.get("status", "")).upper()
        if status == "REFUTED":
            continue
        finding = dict(finding)
        finding["verification"] = status or "UNVERIFIED"
        finding["reportedSeverity"] = str(finding.get("severity", "")).upper()
        if finding["reportedSeverity"] == "BLOCK" and status == "CONFIRMED":
            blockers.append(finding)
        else:
            # An unconfirmed BLOCK carries on as a CONCERN, not as a BLOCK.
            finding["severity"] = "CONCERN"
            concerns.append(finding)

    nits = [f for f in all_findings if str(f.get("severity", "")).upper() == "NIT"]
    failed = sorted(set(expected) - set(lens_results)) + unreadable

    report = {
        "verdict": "BLOCK" if blockers else "CONCERNS" if concerns else "PASS",
        "panel": sorted(lens_results),
        "failedLenses": failed,
        "blockers": merge(blockers),
        "concerns": merge(concerns),
        "nits": merge(nits),
        "good": {lens: (p.get("good", []) if isinstance(p, dict) else [])
                 for lens, p in sorted(lens_results.items())},
    }
    print(json.dumps(report, indent=2, ensure_ascii=False))

    unverified = [f["id"] for f in to_verify if f["id"] not in verdicts]
    notes = []
    if failed:
        notes.append(f"failed lenses: {', '.join(failed)}")
    if unverified:
        notes.append(f"unverified findings kept as concerns: {', '.join(unverified)}")
    if verifier_unreadable:
        notes.append(f"unparseable verifier files: {', '.join(verifier_unreadable)}")
    if notes:
        print("\n" + "; ".join(notes), file=sys.stderr)
    return 0


def main(argv: list[str]) -> int:
    args, expected = [], []
    rest = argv[1:]
    while rest:
        value = rest.pop(0)
        if value == "--expected" and rest:
            expected = [n.strip() for n in rest.pop(0).split(",") if n.strip()]
        else:
            args.append(value)

    if len(args) == 2 and args[0] == "plan":
        return cmd_plan(Path(args[1]))
    if len(args) == 3 and args[0] == "aggregate":
        return cmd_aggregate(Path(args[1]), Path(args[2]), expected)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
