"""Execution-backed scenario evidence: the runner itself runs the mapped tests, the reproductions, and the e2e driver.

Nothing an agent writes about test outcomes counts. The build stage declares, in
`.dev/{plan}/scenario-map.json`, which test ids prove each unit and integration scenario and
which driver case proves each e2e scenario. The gate then:

1. runs exactly those test ids with the contract's test runner, writing results into a fresh
   runner-owned directory, and requires every mapped test to have run and passed;
2. for each `[repro]` scenario, overlays its test files on an isolated worktree at the run's
   base and requires the same tests to fail there on an assertion (not a setup error);
3. starts the contract's services, runs the e2e driver into a fresh output directory, and
   requires a passing `factory.e2e/1` scenario record, with verified evidence files, for every
   mapped case at the exact revision under test; totals are derived from those records.

All three run as guarded executions under the attempt deadline, in the contract's boundary.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import sys
from pathlib import Path

from runner import browser, checkpoints, commands, intent, records, resources, servicehost
from runner import worktree as wt

RENDER_E2E = Path(__file__).resolve().parent.parent / "skills" / "build" / "scripts" / "render-e2e.py"


def present_scenarios(run_dir: Path) -> list[dict]:
    return [s for s in intent.catalog(run_dir) if s.get("present")]


def load_map(ctx, scenarios: list[dict]):
    """(mapping, blocked GateResult or None)."""
    from runner import gates
    path = ctx.plan_dir / "scenario-map.json"
    if not path.is_file():
        return None, gates.blocked(f".dev/{ctx.plan}/scenario-map.json does not exist; map every scenario id in "
                                   "scenarios_file to its test ids or e2e case", code="evidence.map_missing")
    try:
        return records.load_scenario_map(path, scenarios), None
    except records.RecordError as error:
        return None, gates.blocked(f"{error} [{error.code}]", code="evidence.map_invalid")


def command_parts(ctx, contract: dict, command: dict, exec_dir: Path, *, cwd_root: Path,
                  placeholders: dict[str, list[str]]) -> tuple[list[str], dict, Path, str]:
    """(wrapped argv, environment, cwd, boundary) for one contract command record."""
    mode = commands.boundary(command, contract)
    parts = commands.argv(command, cwd_root, placeholders)
    env = commands.environment(command, contract, ctx.env())
    cwd = commands.resolve_inside(cwd_root, command.get("cwd", "."), "cwd")
    wrapped, env = commands.confine(parts, mode, writable=[cwd_root, exec_dir], env=env)
    return wrapped, env, cwd, mode


def results_file(exec_dir: Path, fmt: str) -> Path:
    return exec_dir / "results" / ("junit.xml" if fmt == "junit" else "results.json")


def execute_tests(ctx, contract: dict, test_ids: list[str], *, label: str, root: Path):
    """Run test ids with the contract's test runner. Returns (receipt, results or None, problem or None, results file)."""
    from runner import gates
    tests = contract["tests"]
    exec_dir = gates.next_exec_dir(ctx, label)
    output = results_file(exec_dir, tests["results"])
    output.parent.mkdir(parents=True, exist_ok=True)
    # The runner sees paths from its own working directory; the scenario map names them from the worktree root.
    runner_cwd = tests_cwd(contract)
    values = {"tests": [cwd_relative(test, runner_cwd) for test in test_ids], "results": [str(output)],
              "junit": [str(output)], "out": [str(output.parent)], "run_dir": [str(ctx.run_dir or ctx.attempt_dir)]}
    command = {"id": "tests", **tests["run"]}
    try:
        argv, env, cwd, mode = command_parts(ctx, contract, command, exec_dir, cwd_root=root, placeholders=values)
    except commands.CommandError as error:
        return None, None, f"the contract's test runner cannot run: {error}", output
    receipt = gates.execute(ctx, argv, label=label, kind="tests", cwd=cwd, env=env,
                            timeout_cap=commands.timeout_s(command), boundary=mode, exec_dir=exec_dir)
    if receipt.classification in ("cancelled", "timeout"):
        return receipt, None, None, output
    if not output.is_file():
        tail = gates.first_lines(gates.output_of(receipt)[-1500:], 3)
        return receipt, None, (f"the test runner {gates.describe(receipt)} and wrote no results to its runner-owned "
                               f"file" + (f": {tail}" if tail else "") + gates.full_output(exec_dir)), output
    try:
        results = records.parse_test_results(output, tests["results"])
    except records.RecordError as error:
        return receipt, None, f"{error} [{error.code}]", output
    return receipt, {worktree_relative(test, runner_cwd): outcome for test, outcome in results.items()}, None, output


def tests_cwd(contract: dict) -> str:
    """The test runner's working directory relative to the worktree, POSIX style, "" for the root."""
    cwd = ((contract.get("tests") or {}).get("run") or {}).get("cwd", ".")
    relative = Path(cwd).as_posix().strip("/")
    return "" if relative in ("", ".") else relative


def cwd_relative(test_id: str, cwd: str) -> str:
    """A worktree-relative `path::name` id as the runner sees it from its working directory."""
    if cwd and test_id.startswith(cwd + "/"):
        return test_id[len(cwd) + 1:]
    return test_id


def worktree_relative(test_id: str, cwd: str) -> str:
    """A `path::name` id the runner reported from its working directory, as the scenario map names it."""
    path = records.test_path(test_id)
    looks_like_a_file = path is not None and ("/" in path or "." in path)
    if not cwd or not looks_like_a_file or path.startswith(cwd + "/") or path.startswith("/"):
        return test_id
    return f"{cwd}/{test_id}"


def layer_problems(contract: dict, mapping: dict) -> list[str]:
    """Tests mapped outside their layer's globs; a matter of tidiness the gate reports without failing."""
    layers = (contract.get("tests") or {}).get("layers") or {}
    cwd = tests_cwd(contract)
    problems = []
    for scenario_id, entry in sorted(mapping["scenarios"].items()):
        globs = layers.get(entry["layer"])
        if not globs:
            continue
        for test_id in entry.get("tests", []):
            spellings = {test_id, cwd_relative(test_id, cwd), worktree_relative(test_id, cwd)}
            candidates = [path for path in map(records.test_path, spellings) if path is not None]
            if not any(fnmatch.fnmatch(candidate, pattern) for candidate in candidates for pattern in globs):
                problems.append(f"{scenario_id} maps {test_id} as [{entry['layer']}], but the contract's "
                                f"{entry['layer']} tests live in {', '.join(globs)}")
    return problems


def _same_file(mapped_path: str, result_path: str | None) -> bool:
    """A mapped worktree path names the same file as a result path that may be workspace-relative."""
    if not result_path:
        return False
    return mapped_path == result_path or mapped_path.endswith("/" + result_path)


def resolve_result(test: str, results: dict[str, dict]) -> dict | None:
    """The outcome for one mapped id: an exact `path::name` result, or every collected test of a whole file.

    A file-level id passes only when the file was collected and every test in it passed; one failure,
    error, or skip is reported as the file's outcome, so a file cannot pass on partial evidence.
    """
    exact = results.get(test)
    if exact is not None:
        return exact
    mapped_path = records.test_path(test)
    if mapped_path is None:
        return None
    if "::" in test:
        # A runner that works inside a workspace reports paths relative to it: `ui-shell/src/x.test.tsx` in the
        # map is `src/x.test.tsx` in its results. The name must agree; duplicates are rejected when parsing.
        name = test.split("::", 1)[1]
        hits = [outcome for test_id, outcome in results.items() if "::" in test_id
                and test_id.split("::", 1)[1] == name and _same_file(mapped_path, records.test_path(test_id))]
        return hits[0] if len(hits) == 1 else None
    members = {test_id: outcome for test_id, outcome in results.items()
               if _same_file(mapped_path, records.test_path(test_id))}
    if not members:
        return None
    ranked = sorted(members.items(), key=lambda item: {"error": 0, "failed": 1, "skipped": 2}.get(item[1]["outcome"], 3))
    worst_id, worst = ranked[0]
    if worst["outcome"] == "passed":
        return {"outcome": "passed", "detail": f"{len(members)} test(s) in the file passed"}
    return {"outcome": worst["outcome"], "detail": f"{worst_id.split('::', 1)[-1]}" + (f": {worst['detail']}" if worst.get("detail") else "")}


def scenario_outcomes(wanted: dict[str, list[str]], results: dict[str, dict]) -> tuple[dict, list[str]]:
    """Per-scenario outcomes for the mapped ids and the failures they imply, in scenario order."""
    outcomes_by_scenario: dict = {}
    failures: list[str] = []
    for scenario_id, tests in sorted(wanted.items()):
        outcomes = {}
        for test in tests:
            result = resolve_result(test, results)
            outcome = result["outcome"] if result else "not run"
            outcomes[test] = outcome
            if outcome == "not run":
                failures.append(f"{scenario_id}: {test} was not executed (no such test, or not collected)")
            elif outcome == "skipped":
                failures.append(f"{scenario_id}: {test} was skipped" + (f" ({result['detail']})" if result.get("detail") else ""))
            elif outcome != "passed":
                failures.append(f"{scenario_id}: {test} {outcome}" + (f": {result['detail']}" if result.get("detail") else ""))
        outcomes_by_scenario[scenario_id] = outcomes
    return outcomes_by_scenario, failures


def check_tests(ctx, contract: dict, mapping: dict, data: dict):
    from runner import gates
    wanted = {sid: entry["tests"] for sid, entry in mapping["scenarios"].items() if entry["layer"] != "e2e"}
    if not wanted:
        return None
    if "tests" not in contract:
        return gates.blocked("scenarios need test execution but the contract declares no 'tests' runner",
                             retryable=False, code="contract.gap", **data)
    ctx.warnings.extend(layer_problems(contract, mapping))
    ids = sorted({test for tests in wanted.values() for test in tests})
    inputs = {"tree": checkpoints.tree(ctx.worktree), "tests": wanted, "contract_tests": contract["tests"],
              "runner": ctx.runner_sha()}
    reused, decision = checkpoints.reusable(ctx.run_dir, "tests", inputs)
    checkpoints.note(ctx, "tests", decision)
    if reused is not None:
        data["tests"] = {**reused["data"], "reused_from": reused["completed_at"]}
        return None
    receipt, results, problem, output = execute_tests(ctx, contract, ids, label="tests", root=ctx.worktree)
    if receipt is not None:
        halted = gates.interrupted(ctx, receipt, "the mapped tests", **data)
        if halted:
            return halted
    if problem:
        return gates.blocked(problem, code="evidence.tests_unrun", **data)
    summary: dict = {"results": str(output), "scenarios": {}}
    outcomes_by_scenario, failures = scenario_outcomes(wanted, results)
    summary["scenarios"] = outcomes_by_scenario
    summary["runner_exit"] = receipt.exit_code
    data["tests"] = summary
    if failures:
        return gates.blocked("; ".join(failures[:5]) + (f" (+{len(failures) - 5} more)" if len(failures) > 5 else ""),
                             code="evidence.tests_failed", **data)
    if receipt.classification != "succeeded":
        return gates.blocked(f"every mapped test passed, but the test runner {gates.describe(receipt)}",
                             code="evidence.tests_failed", **data)
    checkpoints.save(ctx.run_dir, "tests", inputs, revision=wt.head(ctx.worktree), outputs=[output],
                     receipt=str(output.parent.parent / "receipt.json"), data=summary)
    checkpoints.save_verified_scenario_map(ctx.run_dir, mapping, revision=wt.head(ctx.worktree))
    return None


def check_repro(ctx, contract: dict, mapping: dict, scenarios: list[dict], data: dict):
    """Each [repro] scenario's tests must fail on an assertion at the base and pass on the change."""
    from runner import gates
    repro = [s for s in scenarios if s.get("repro")]
    if not repro:
        return None
    if not ctx.base_sha:
        return gates.blocked("a [repro] scenario needs the run's base revision", retryable=False, code="evidence.repro",
                             **data)
    problems, ids, files = [], [], set()
    for scenario in repro:
        entry = mapping["scenarios"][scenario["id"]]
        if scenario["layer"] == "e2e":
            problems.append(f"{scenario['id']} is an e2e [repro] scenario; reproductions run as unit or integration tests")
            continue
        for test in entry["tests"]:
            path = records.test_path(test)
            if path is None:
                problems.append(f"{scenario['id']}: {test} has no file part, so it cannot be overlaid on the base")
            else:
                ids.append(test)
                files.add(path)
    if problems:
        return gates.blocked("; ".join(problems), retryable=False, code="evidence.repro", **data)
    inputs = {"tree": checkpoints.tree(ctx.worktree), "base": ctx.base_sha, "tests": sorted(set(ids)),
              "contract_tests": contract["tests"], "runner": ctx.runner_sha()}
    reused, decision = checkpoints.reusable(ctx.run_dir, "repro", inputs)
    checkpoints.note(ctx, "repro", decision)
    if reused is not None:
        data["repro"] = {**reused["data"], "reused_from": reused["completed_at"]}
        return None
    exec_dir = gates.next_exec_dir(ctx, "repro-base")
    base = exec_dir / "base"
    exec_dir.mkdir(parents=True, exist_ok=True)
    try:
        wt.git("-C", str(ctx.worktree), "worktree", "add", "--quiet", "--detach", str(base), ctx.base_sha)
        for relative in sorted(files):
            source = ctx.worktree / relative
            target = base / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        setup = gates.run_setup(ctx, contract, data, root=base, label="repro-setup")
        if setup is not None:
            setup.reason = f"preparing the base revision for [repro] scenarios: {setup.reason}"
            return setup
        receipt, results, problem, _ = execute_tests(ctx, contract, sorted(set(ids)), label="repro-tests", root=base)
    except (wt.GitError, OSError) as error:
        return gates.blocked(f"could not prepare the base worktree for [repro] scenarios: {error}", code="evidence.repro",
                             **data)
    finally:
        if base.exists():
            wt.git("-C", str(ctx.worktree), "worktree", "remove", "--force", str(base), check=False)
    if receipt is not None:
        halted = gates.interrupted(ctx, receipt, "the [repro] tests on the base", **data)
        if halted:
            return halted
    if problem and results is None:
        return gates.blocked(f"on the base revision {ctx.base_sha[:12]}: {problem}", code="evidence.repro", **data)
    failures, record = [], {}
    for scenario in repro:
        for test in mapping["scenarios"][scenario["id"]]["tests"]:
            outcome = (results.get(test) or {}).get("outcome", "not run")
            record[test] = outcome
            if outcome == "passed":
                failures.append(f"{scenario['id']}: {test} passes on the base {ctx.base_sha[:12]}, so it does not "
                                "reproduce the bug")
            elif outcome != "failed":
                failures.append(f"{scenario['id']}: {test} is {outcome} on the base {ctx.base_sha[:12]}, which is "
                                "a setup failure, not a reproduction")
    data["repro"] = {"base": ctx.base_sha, "outcomes_on_base": record}
    if failures:
        return gates.blocked("; ".join(failures[:5]), code="evidence.repro", **data)
    checkpoints.save(ctx.run_dir, "repro", inputs, revision=wt.head(ctx.worktree), outputs=[], receipt=str(exec_dir),
                     data=data["repro"])
    return None


def check_e2e(ctx, contract: dict, mapping: dict, scenarios: list[dict], data: dict):
    from runner import gates
    cases = {sid: entry["e2e_case"] for sid, entry in sorted(mapping["scenarios"].items()) if entry["layer"] == "e2e"}
    if not cases:
        return None
    e2e = contract.get("e2e")
    if e2e is None or "none" in e2e:
        reason = f" ({e2e['none']})" if e2e else ""
        return gates.blocked(f"the plan has e2e scenarios but the contract declares no e2e driver{reason}",
                             retryable=False, code="contract.gap", **data)
    inputs = {"tree": checkpoints.tree(ctx.worktree), "cases": cases, "e2e": e2e,
              "services": contract.get("services", []), "ports": contract.get("ports", []), "runner": ctx.runner_sha()}
    reused, decision = checkpoints.reusable(ctx.run_dir, "e2e", inputs)
    checkpoints.note(ctx, "e2e", decision)
    if reused is not None:
        data["e2e"] = {**reused["data"]["e2e"], "reused_from": reused["completed_at"]}
        data["e2e_report"] = reused["data"]["e2e_report"]
        return None
    exec_dir = gates.next_exec_dir(ctx, "e2e")
    out = exec_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    revision = wt.head(ctx.worktree)
    lease_home = ctx.home or ctx.run_dir or ctx.attempt_dir
    ports, port_leases = {}, []
    try:
        for name in contract.get("ports", []):
            port, lease = resources.acquire_port(lease_home, f"{ctx.plan}:e2e:{name}", *ctx.port_range)
            ports[name] = port
            port_leases.append(lease)
    except RuntimeError as error:
        for lease in port_leases:
            lease.release()
        return gates.blocked(f"no port available for the e2e services: {error}", code="resources.ports", **data)
    service_data = exec_dir / "service-data"
    service_data.mkdir(parents=True, exist_ok=True)
    values = {"out": [str(out)], "run_dir": [str(ctx.run_dir or ctx.attempt_dir)], "tests": [], "results": [],
              "junit": [], **{f"port_{name}": [str(port)] for name, port in ports.items()}}
    extra = {"FACTORY_E2E_OUT": str(out), "FACTORY_REVISION": revision, "FACTORY_SERVICE_DATA": str(service_data),
             **{f"FACTORY_PORT_{name.upper()}": str(port) for name, port in ports.items()}}
    driver = {"id": "e2e", **e2e["driver"]}
    mode = commands.boundary(driver, contract)
    hosted, browser_record = None, {"status": "off"}
    if ctx.browser != "off":
        # The driver gets the browser the build agent had, so it behaves the same at the gate as in the attempt.
        try:
            hosted = browser.launch(home=lease_home, holder=f"{ctx.plan}:e2e:browser", port_range=ctx.port_range,
                                    mode=mode, directory=exec_dir / "browser", env=ctx.env())
            browser_record = browser.record(hosted)
        except browser.Unavailable as error:
            browser_record = {"status": "unavailable", "reason": str(error)}
        extra.update(browser.environment(hosted))
    try:
        plan = {"log_dir": str(exec_dir / "services"), "services": [], "driver": {}}
        services = {service["id"]: service for service in contract.get("services", [])}
        for service_id in e2e.get("services", []):
            service = services[service_id]
            ready = {"id": f"{service_id}-ready", **service["ready"]}
            plan["services"].append({
                "id": service_id, "timeout_s": commands.timeout_s(service) if "timeout_s" in service else 60,
                "argv": commands.argv(service, ctx.worktree, values),
                "env": {**commands.environment(service, contract, ctx.env()), **extra},
                "cwd": str(commands.resolve_inside(ctx.worktree, service.get("cwd", "."), "cwd")),
                "ready": {"argv": commands.argv(ready, ctx.worktree, values),
                          "env": {**commands.environment(ready, contract, ctx.env()), **extra},
                          "cwd": str(commands.resolve_inside(ctx.worktree, ready.get("cwd", "."), "cwd"))},
            })
        plan["driver"] = {"argv": commands.argv(driver, ctx.worktree, values),
                          "env": {**commands.environment(driver, contract, ctx.env()), **extra},
                          "cwd": str(commands.resolve_inside(ctx.worktree, driver.get("cwd", "."), "cwd"))}
        caches, cache_paths = commands.tool_caches(ctx.env(), mode)
        for record in [*plan["services"], *[s["ready"] for s in plan["services"]], plan["driver"]]:
            record["env"] = {**caches, **record["env"]}
        plan_path = exec_dir / "services.json"
        plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        argv = commands.sandbox_wrap(servicehost.bootstrap_argv(plan_path), mode,
                                     writable=[ctx.worktree, exec_dir, *cache_paths])
    except commands.CommandError as error:
        browser.stop(hosted)
        for lease in port_leases:
            lease.release()
        return gates.blocked(f"the e2e driver cannot run: {error}", retryable=False, code="contract.unrunnable", **data)
    base_env = {name: os.environ[name] for name in commands.BASELINE_ENV if name in os.environ}
    try:
        receipt = gates.execute(ctx, argv, label="e2e", kind="e2e", cwd=ctx.worktree, env=base_env,
                                timeout_cap=commands.timeout_s(driver), boundary=mode, exec_dir=exec_dir,
                                extra_fds=[lease.fd for lease in port_leases])
    finally:
        browser.stop(hosted)
        for lease in port_leases:
            lease.release()
    data.setdefault("e2e", {}).update({"ports": ports, "browser": browser_record})
    halted = gates.interrupted(ctx, receipt, "the e2e driver", **data)
    if halted:
        return halted
    record_path = out / "e2e.json"
    if not record_path.is_file():
        tail = gates.first_lines(gates.output_of(receipt)[-1500:], 3)
        return gates.blocked(f"the e2e driver {gates.describe(receipt)} and wrote no factory.e2e/1 record to "
                             "$FACTORY_E2E_OUT/e2e.json" + (f": {tail}" if tail else ""), code="e2e.missing", **data)
    try:
        record = records.load_e2e(record_path)
    except records.RecordError as error:
        return gates.blocked(f"{error} [{error.code}]", code="e2e.invalid", **data)
    problems = records.verify_e2e_evidence(record, out)
    if record.get("revision") != revision:
        problems.append(f"the e2e record names revision {record.get('revision')!r}, not the tested {revision[:12]}")
    by_case = {scenario["id"]: scenario for scenario in record["scenarios"]}
    coverage = {}
    for scenario_id, case in cases.items():
        scenario = by_case.get(case)
        if scenario is None:
            problems.append(f"{scenario_id}: the driver recorded no case {case!r}")
            coverage[scenario_id] = "missing"
            continue
        failed = scenario["status"] != "pass" or not all(a["passed"] for a in scenario.get("assertions", []))
        coverage[scenario_id] = "failed" if failed else "passed"
        if failed:
            problems.append(f"{scenario_id}: case {case!r} failed")
        if record["kind"] == "frontend" and not scenario.get("screenshots"):
            problems.append(f"{scenario_id}: frontend case {case!r} captured no screenshot")
        if record["kind"] == "non-frontend" and not scenario.get("states"):
            problems.append(f"{scenario_id}: non-frontend case {case!r} captured no before/after state")
    if receipt.classification != "succeeded" and not problems:
        problems.append(f"the e2e driver {gates.describe(receipt)}")
    counts = records.e2e_counts({"scenarios": [by_case[c] for c in cases.values() if c in by_case]})
    data["e2e"] = {"revision": revision, "coverage": coverage, "counts": counts, "record": str(record_path),
                   "ports": ports, "service_data": str(service_data), "browser": browser_record}
    if problems:
        return gates.blocked("; ".join(problems[:5]), code="e2e.failed", **data)
    data["e2e_report"] = publish_e2e(ctx, record, out)
    published = ctx.report_dir / f"{ctx.plan}-e2e.json"
    evidence = [path for path in (ctx.report_dir / f"{ctx.plan}-e2e").rglob("*") if path.is_file()]
    checkpoints.save(ctx.run_dir, "e2e", inputs, revision=revision, outputs=[published, *evidence],
                     receipt=str(exec_dir / "receipt.json"), data={"e2e": data["e2e"], "e2e_report": data["e2e_report"]})
    return None


def publish_e2e(ctx, record: dict, out: Path) -> str:
    """Copy the runner-verified record and its evidence into the report directory and render its HTML."""
    from runner import gates
    folder = ctx.report_dir / f"{ctx.plan}-e2e"
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(out, folder)
    published = json.loads(json.dumps(record))
    for scenario in published["scenarios"]:
        for shot in scenario.get("screenshots", []):
            shot["file"] = f"{folder.name}/{shot['file']}"
    (folder / "e2e.json").unlink(missing_ok=True)
    sidecar = ctx.report_dir / f"{ctx.plan}-e2e.json"
    sidecar.write_text(json.dumps(published, indent=2) + "\n", encoding="utf-8")
    gates.run_script(ctx, [sys.executable, str(RENDER_E2E), str(sidecar), "--out",
                           str(ctx.report_dir / f"{ctx.plan}-e2e-report.html")], "render-e2e")
    return gates.relative(sidecar, ctx.report_dir.parent)
