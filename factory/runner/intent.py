"""Approved intent and attempt handoffs: what the operator approved, and exactly what each attempt saw.

At the scope handoff the runner seals a snapshot of the approved request, non-goals, and
acceptance scenarios (with stable ids) under the runner-owned `intent/` directory, which no
stage sandbox can write. Unattended stages may refine the technical spec, but a later gate
compares the locked fields mechanically: an approved scenario's layer and requirement text,
and each non-goal, must still be present. Added scenarios get new ids. Every attempt keeps
copies of the plan inputs it started from and a manifest of what it produced.

Layout (all paths relative to the run directory, so an archived run keeps them):
  intent/approved.json            current approved intent (factory.intent/1), content-hashed
  intent/versions/{v}/            each handoff's approved.json, spec.md, contract.json, request.md
  intent/scenarios.json           every scenario id ever assigned, approved or added, and where it came from
  intent/deltas/{stage}-{n}.json  decision and scenario changes one attempt made to the spec
  attempts/{stage}-{n}/inputs/    the plan files the attempt started from, with manifest.json
  attempts/{stage}-{n}/outputs.json  hashes of the plan files after the attempt
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from runner import records
from runner.model import Run, atomic_write, utc_now

DIR = "intent"
INPUT_FILES = ("spec.md", "implementation-notes.md", "pr.md", "scenario-map.json")
INPUT_PATTERNS = ("spec-review_*.md", "review_*.md")


class IntentError(Exception):
    """The approved intent is missing, tampered, or no longer satisfied; `repair` names the next step."""

    def __init__(self, message: str, repair: str | None = None, code: str = "intent.invalid"):
        super().__init__(message)
        self.repair = repair
        self.code = code


def intent_dir(run_dir: Path) -> Path:
    return Path(run_dir) / DIR


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def sealed(body: dict) -> dict:
    return {**body, "content_sha256": records.canonical_sha256(body)}


def key(scenario: dict) -> tuple[str, str]:
    return scenario["layer"], scenario["requirement"]


# --- the handoff snapshot ---------------------------------------------------------------

def snapshot_handoff(run: Run) -> dict:
    """Seal the approved intent at a scope handoff; ids of scenarios approved before are kept. Caller saves run."""
    request = Path(run.data["request_file"]) if run.data.get("request_file") else None
    document = seal(run.dir, run_id=run.id, spec=run.plan_dir / "spec.md",
                    contract=run.worktree / records.CONTRACT_PATH, request_file=request,
                    request=run.data.get("request"), base_sha=run.data.get("base_sha"))
    run.data["intent"] = {"version": document["version"], "path": f"{DIR}/approved.json",
                          "sha256": document["content_sha256"], "scenarios": len(document["scenarios"]),
                          "non_goals": len(document["non_goals"])}
    return document


def seal(run_dir: Path, *, run_id: str, spec: Path, contract: Path, request_file: Path | None, request: str | None,
         base_sha: str | None) -> dict:
    """Write the next approved-intent version under run_dir/intent and return it."""
    spec_text = spec.read_text(encoding="utf-8")
    catalog = read_json(intent_dir(run_dir) / "scenarios.json") or {"scenarios": []}
    known = {(s["layer"], s["requirement"]): s for s in catalog["scenarios"]}
    approved: list[dict] = []
    for scenario in records.parse_scenarios(spec_text):
        entry = known.get(key(scenario))
        if entry is None:
            entry = {"id": f"S{len(catalog['scenarios']) + 1}", "layer": scenario["layer"],
                     "requirement": scenario["requirement"], "origin": "scope"}
            catalog["scenarios"].append(entry)
            known[key(scenario)] = entry
        entry.update({"approved": True, "present": True, "change_set": scenario["change_set"],
                      "repro": scenario["repro"]})
        approved.append({"id": entry["id"], "change_set": scenario["change_set"], "layer": scenario["layer"],
                         "requirement": scenario["requirement"], "repro": scenario["repro"]})
    approved_keys = {(s["layer"], s["requirement"]) for s in approved}
    for entry in catalog["scenarios"]:
        if (entry["layer"], entry["requirement"]) not in approved_keys:
            entry.update({"approved": False, "present": False})
    version = int((read_json(intent_dir(run_dir) / "approved.json") or {}).get("version", 0)) + 1
    body = {
        "schema": records.INTENT_SCHEMA,
        "run_id": run_id,
        "version": version,
        "handoff_at": utc_now(),
        "base_sha": base_sha,
        "request": request,
        "request_sha256": records.sha256_file(request_file) if request_file and request_file.is_file() else None,
        "non_goals": records.parse_non_goals(spec_text),
        "scenarios": approved,
        "spec_sha256": records.sha256_file(spec),
        "contract_sha256": records.sha256_file(contract) if contract.is_file() else None,
    }
    document = sealed(body)
    version_dir = intent_dir(run_dir) / "versions" / str(version)
    version_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(spec, version_dir / "spec.md")
    if contract.is_file():
        shutil.copyfile(contract, version_dir / "contract.json")
    if request_file and request_file.is_file():
        shutil.copyfile(request_file, version_dir / "request.md")
    write_json(version_dir / "approved.json", document)
    write_json(intent_dir(run_dir) / "scenarios.json", catalog)
    write_json(intent_dir(run_dir) / "approved.json", document)
    return document


def load_approved(run_dir: Path, expected_sha256: str | None = None) -> dict:
    """The sealed approved intent, verified; raises IntentError with a repair step."""
    path = intent_dir(run_dir) / "approved.json"
    repair = f"reopen scope with `factory retry {Path(run_dir).name} --rescope` and hand off again"
    if not path.is_file():
        raise IntentError("the run has no approved-intent snapshot (it predates snapshots or the file is gone)",
                          repair, code="intent.missing")
    document = read_json(path)
    if not isinstance(document, dict) or document.get("schema") != records.INTENT_SCHEMA:
        raise IntentError(f"{path} is not a {records.INTENT_SCHEMA} record", repair, code="intent.tampered")
    body = {k: v for k, v in document.items() if k != "content_sha256"}
    if records.canonical_sha256(body) != document.get("content_sha256"):
        raise IntentError(f"{path} does not match its content hash", repair, code="intent.tampered")
    if expected_sha256 and document["content_sha256"] != expected_sha256:
        raise IntentError(f"{path} is not the intent recorded in run.json at handoff", repair, code="intent.tampered")
    version_dir = intent_dir(run_dir) / "versions" / str(document["version"])
    spec = version_dir / "spec.md"
    if not spec.is_file() or records.sha256_file(spec) != document["spec_sha256"]:
        raise IntentError(f"the approved spec snapshot {spec} is missing or altered", repair, code="intent.tampered")
    contract = version_dir / "contract.json"
    if document.get("contract_sha256") and (not contract.is_file()
                                            or records.sha256_file(contract) != document["contract_sha256"]):
        raise IntentError(f"the approved contract snapshot {contract} is missing or altered", repair,
                          code="intent.tampered")
    return document


def approved_contract(run_dir: Path, document: dict) -> Path | None:
    path = intent_dir(run_dir) / "versions" / str(document["version"]) / "contract.json"
    return path if path.is_file() else None


# --- drift ---------------------------------------------------------------------------------

def check_spec(run_dir: Path, document: dict, spec_text: str, *, stage: str, attempt: int) -> list[str]:
    """Locked-field problems in the current spec; registers added scenarios with new ids."""
    current = records.parse_scenarios(spec_text)
    present = {key(s): s for s in current}
    problems = []
    for scenario in document["scenarios"]:
        if key(scenario) not in present:
            problems.append(f"approved scenario {scenario['id']} ([{scenario['layer']}] {scenario['requirement']}) "
                            "was removed or reworded")
        elif present[key(scenario)]["repro"] != scenario["repro"]:
            problems.append(f"approved scenario {scenario['id']} changed its [repro] tag")
    non_goals = set(records.parse_non_goals(spec_text))
    for goal in document["non_goals"]:
        if goal not in non_goals:
            problems.append(f"approved non-goal was removed or reworded: {goal}")
    register(run_dir, current, stage=stage, attempt=attempt)
    return problems


def register(run_dir: Path, current: list[dict], *, stage: str, attempt: int) -> None:
    path = intent_dir(run_dir) / "scenarios.json"
    catalog = read_json(path) or {"scenarios": []}
    known = {(s["layer"], s["requirement"]): s for s in catalog["scenarios"]}
    present = {key(s): s for s in current}
    changed = False
    for scenario in current:
        entry = known.get(key(scenario))
        if entry is None:
            entry = {"id": f"S{len(catalog['scenarios']) + 1}", "layer": scenario["layer"],
                     "requirement": scenario["requirement"], "approved": False,
                     "origin": f"{stage} attempt {attempt}"}
            catalog["scenarios"].append(entry)
            known[key(scenario)] = entry
            changed = True
        values = {"present": True, "change_set": scenario["change_set"], "repro": scenario["repro"]}
        if any(entry.get(k) != v for k, v in values.items()):
            entry.update(values)
            changed = True
    for entry in catalog["scenarios"]:
        if (entry["layer"], entry["requirement"]) not in present and entry.get("present"):
            entry["present"] = False
            changed = True
    if changed:
        write_json(path, catalog)


def catalog(run_dir: Path) -> list[dict]:
    return (read_json(intent_dir(run_dir) / "scenarios.json") or {"scenarios": []})["scenarios"]


# --- attempt handoffs ---------------------------------------------------------------------------

def plan_files(plan_dir: Path) -> list[Path]:
    files = [plan_dir / name for name in INPUT_FILES if (plan_dir / name).is_file()]
    for pattern in INPUT_PATTERNS:
        files += sorted(plan_dir.glob(pattern), key=lambda p: int(re.findall(r"\d+", p.stem)[-1]))
    return files


def manifest(files: list[Path], root: Path) -> dict:
    return {path.relative_to(root).as_posix(): {"sha256": records.sha256_file(path), "bytes": path.stat().st_size}
            for path in files}


def snapshot_inputs(run: Run, stage: str, n: int, *, head: str, context_file: Path) -> dict:
    """Copy the plan files an attempt starts from, and record what else defined its inputs."""
    inputs = run.attempt_dir(stage, n) / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    files = plan_files(run.plan_dir)
    for path in files:
        shutil.copyfile(path, inputs / path.name)
    if context_file.is_file():
        shutil.copyfile(context_file, inputs / "factory-run.json")
    document = read_json(intent_dir(run.dir) / "approved.json") or {}
    record = {
        "schema": "factory.attempt-inputs/1",
        "run_id": run.id, "stage": stage, "attempt": n, "recorded_at": utc_now(),
        "source_revision": head,
        "intent_sha256": document.get("content_sha256"),
        "contract_sha256": document.get("contract_sha256"),
        "files": manifest([inputs / p.name for p in files], inputs),
        "context_sha256": records.sha256_file(inputs / "factory-run.json") if context_file.is_file() else None,
    }
    write_json(inputs / "manifest.json", record)
    return record


def snapshot_outputs(run: Run, stage: str, n: int, *, head: str | None) -> dict:
    record = {"schema": "factory.attempt-outputs/1", "run_id": run.id, "stage": stage, "attempt": n,
              "recorded_at": utc_now(), "source_revision": head,
              "files": manifest(plan_files(run.plan_dir), run.plan_dir)}
    write_json(run.attempt_dir(stage, n) / "outputs.json", record)
    return record


def decision_delta(run: Run, stage: str, n: int, *, auto_decided: list[str]) -> dict | None:
    """What an attempt changed in the spec's decisions and scenarios, for audit; None when nothing changed."""
    before_path = run.attempt_dir(stage, n) / "inputs" / "spec.md"
    after_path = run.plan_dir / "spec.md"
    if not before_path.is_file() or not after_path.is_file():
        return None
    before_text = before_path.read_text(encoding="utf-8")
    after_text = after_path.read_text(encoding="utf-8")
    if before_text == after_text:
        return None
    before, after = records.parse_decisions(before_text), records.parse_decisions(after_text)
    changed = [{"slug": slug, "before": before[slug], "after": after[slug]}
               for slug in sorted(set(before) & set(after)) if before[slug] != after[slug]]
    added = [{"slug": slug, "after": after[slug]} for slug in sorted(set(after) - set(before))]
    removed = [{"slug": slug, "before": before[slug]} for slug in sorted(set(before) - set(after))]
    slugs = {entry["slug"] for entry in changed + added + removed}
    affected = []
    ids = {(s["layer"], s["requirement"]): s["id"] for s in catalog(run.dir)}
    plan = "\n".join(line for line in after_text.splitlines())
    for scenario in records.parse_scenarios(after_text):
        block = change_set_text(plan, scenario["change_set"])
        if any(slug in block for slug in slugs):
            affected.append(ids.get(key(scenario), "?"))
    before_keys = {key(s) for s in records.parse_scenarios(before_text)}
    after_keys = {key(s) for s in records.parse_scenarios(after_text)}
    delta = {
        "schema": "factory.decision-delta/1", "run_id": run.id, "stage": stage, "attempt": n, "recorded_at": utc_now(),
        "decisions": {"changed": changed, "added": added, "removed": removed},
        "scenarios": {"added": sorted(ids.get(k, "?") for k in after_keys - before_keys),
                      "removed": sorted(f"[{k[0]}] {k[1]}" for k in before_keys - after_keys)},
        "affected_scenarios": sorted(set(affected)),
        "auto_decided": auto_decided,
    }
    write_json(intent_dir(run.dir) / "deltas" / f"{stage}-{n}.json", delta)
    return delta


def change_set_text(plan: str, number: int) -> str:
    match = re.search(rf"^\s*{number}\.\s.*?(?=^\s*\d+\.\s|\Z)", plan, re.M | re.S)
    return match.group(0) if match else ""


def auto_decisions(text: str) -> list[str]:
    return [records.normalize_text(line) for line in text.splitlines()
            if "auto-decided" in line.lower() and ("answered:" in line.lower() or "deviations" in line.lower())]
