"""Typed park conditions across attempts: recorded by the worker, re-checked where objective, resolved explicitly.

A stage reports a condition from the fixed park list in its schema-2 result. The worker
(the only writer of run.json) records it under `run.data["conditions"]` with a stable id.
The stage that raised it cannot complete while it is unresolved, whatever its gate says.
On a later attempt the runner re-checks what it can observe itself (a required
environment name, or a launch problem its own passing gate disproves; the latter also
applies in the attempt that reports it); otherwise the stage must report an explicit
resolution, with evidence, naming the condition id. An objective re-check that still fails overrides a
claimed resolution.
"""

from __future__ import annotations

from runner import records
from runner.model import Run, utc_now


def recorded(run: Run) -> list[dict]:
    return run.data.setdefault("conditions", [])


def open_for(run: Run, stage: str) -> list[dict]:
    return [c for c in run.data.get("conditions", []) if c["stage"] == stage and c["status"] == "unresolved"]


def superseded(code: str, gate) -> str | None:
    """Evidence that the runner's own passing gate makes a reported condition moot, or None."""
    if code == "launch.unavailable" and gate is not None and gate.passed:
        e2e = (gate.data or {}).get("e2e")
        if e2e:
            return (f"the runner's e2e driver passed at the gate on {str(e2e.get('revision', ''))[:12]} "
                    f"({e2e.get('counts', {}).get('passed', 'all')} scenario(s))")
        return "the gate passed and the plan has no e2e scenario that needs a launch"
    return None


def recheck(condition: dict, env: dict, gate=None) -> tuple[bool | None, str]:
    """(True resolved, False still failing, None not objectively checkable) and what was observed."""
    moot = superseded(condition["code"], gate)
    if moot:
        return True, moot
    names = condition.get("requires_env") or []
    if condition["code"] == "environment.missing_credentials" and names:
        missing = [name for name in names if not env.get(name)]
        if missing:
            return False, f"still unset in the runner's environment: {', '.join(missing)}"
        return True, f"now set in the runner's environment: {', '.join(names)}"
    return None, "not objectively checkable by the runner"


def evaluate(run: Run, stage: str, attempt: dict, result: dict | None, env: dict,
             gate=None) -> tuple[list[dict], list[str]]:
    """Apply one finished attempt's report to the run's conditions.

    Returns (unresolved conditions blocking this stage, warnings about the report).
    """
    now = utc_now()
    n = attempt["n"]
    reported = (result or {}).get("conditions", [])
    claims = {c["resolves"]: c for c in reported if c.get("resolution") == "resolved"}
    warnings: list[str] = []
    known = {c["id"] for c in recorded(run)}
    for claimed in claims:
        if claimed not in known:
            warnings.append(f"resolution names unknown condition {claimed}")
    for condition in open_for(run, stage):
        if condition["attempt"] == n:
            continue
        observed, detail = recheck(condition, env, gate)
        claim = claims.get(condition["id"])
        condition.setdefault("checks", []).append({"attempt": n, "at": now, "observed": detail,
                                                   "claimed": claim is not None})
        if observed is True:
            resolve(condition, attempt=n, by="runner", evidence=[detail], now=now)
        elif observed is None and claim is not None:
            resolve(condition, attempt=n, by="skill", evidence=list(claim.get("evidence", [])), now=now)
        elif observed is False and claim is not None:
            warnings.append(f"{condition['id']} claimed resolved, but the runner's re-check found it {detail}")
    for condition in reported:
        if condition.get("resolution", "unresolved") != "unresolved":
            continue
        spec = records.CONDITION_CODES[condition["code"]]
        duplicate = next((c for c in open_for(run, stage) if c["code"] == condition["code"]
                          and c["summary"] == condition["summary"]), None)
        if duplicate is not None:
            duplicate["last_reported_attempt"] = n
            moot = superseded(condition["code"], gate)
            if moot:
                resolve(duplicate, attempt=n, by="runner", evidence=[moot], now=now)
            continue
        entry = {
            "id": f"C{len(recorded(run)) + 1}",
            "code": condition["code"],
            "category": spec["category"],
            "retryable": spec["retryable"],
            "summary": condition["summary"],
            "evidence": list(condition.get("evidence", [])),
            "requires_env": list(condition.get("requires_env", [])),
            "stage": stage,
            "attempt": n,
            "raised_at": now,
            "status": "unresolved",
            "resolution": None,
            "checks": [],
        }
        recorded(run).append(entry)
        moot = superseded(condition["code"], gate)
        if moot:
            resolve(entry, attempt=n, by="runner", evidence=[moot], now=now)
            warnings.append(f"{entry['id']} {entry['code']} was reported but resolved by the runner: {moot}")
    return open_for(run, stage), warnings


def resolve(condition: dict, *, attempt: int, by: str, evidence: list[str], now: str) -> None:
    condition["status"] = "resolved"
    condition["resolution"] = {"attempt": attempt, "by": by, "evidence": evidence, "at": now}


def next_action(condition: dict, run_id: str) -> str:
    return records.CONDITION_CODES[condition["code"]]["next"].replace("{run}", run_id)


def context(run: Run, stage: str) -> list[dict]:
    """The open conditions a new attempt must resolve or report again, for factory-run.json."""
    return [{"id": c["id"], "code": c["code"], "summary": c["summary"], "evidence": c["evidence"],
             "requires_env": c["requires_env"], "raised": f"{c['stage']} attempt {c['attempt']}"}
            for c in open_for(run, stage)]
