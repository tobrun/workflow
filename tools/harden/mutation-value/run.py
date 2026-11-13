"""Run isolated, fresh-cache mutation comparisons and separate seeded faults."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent


def command(args, cwd, log, acceptable=(0,)):
    start = time.perf_counter()
    result = subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, timeout=300,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
             "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
    )
    log.write_text(result.stdout)
    if result.returncode not in acceptable:
        raise RuntimeError(f"{args}: exit {result.returncode}; see {log}")
    return time.perf_counter() - start, result.returncode


def run(output, suites, projects):
    output.mkdir(parents=True, exist_ok=True)
    faults = json.loads((ROOT / "faults.json").read_text())
    data = {
        "python": sys.version, "platform": platform.platform(),
        "protocol": "Fresh cache per arm; six extra tests per main expansion; two in score_only.",
        "faults_sha256": hashlib.sha256((ROOT / "faults.json").read_bytes()).hexdigest(),
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("mutmut", "pytest", "coverage", "parso", "pony")
        },
        "results": [],
    }
    for project in projects:
        source = ROOT / "projects" / project
        original = (source / "app.py").read_text()
        for suite in suites:
            work = output / f"{project}-{suite}"
            if work.exists():
                raise RuntimeError(f"Refusing to overwrite previous run: {work}")
            work.mkdir()
            (work / "app.py").write_text(original)
            shutil.copy(source / "baseline.py", work / "test_baseline.py")
            if suite != "baseline":
                shutil.copy(source / f"{suite}.py", work / "test_extra.py")
            # Warm once, then three separately started ordinary runs.
            command([sys.executable, "-m", "pytest", "-q"], work, work / "warmup.log")
            import re
            collected = re.search(r"(\d+) passed", (work / "warmup.log").read_text())
            assert collected, "Test count not found"
            timings = [
                command([sys.executable, "-m", "pytest", "-q"], work,
                        work / f"tests-{i}.log")[0]
                for i in range(3)
            ]
            command([sys.executable, "-m", "coverage", "run", "--branch",
                     "--source=app", "-m", "pytest", "-q"],
                    work, work / "coverage-run.log")
            command([sys.executable, "-m", "coverage", "json", "-o", "coverage.json"],
                    work, work / "coverage-export.log")
            cov = json.loads((work / "coverage.json").read_text())["totals"]
            print(f"Mutating {project}/{suite}", flush=True)
            mutation_seconds, _ = command(
                [str(Path(sys.executable).parent / "mutmut"), "run",
                 "--paths-to-mutate=app.py", "--tests-dir=.",
                 f"--runner={sys.executable} -m pytest -x -q",
                 "--no-progress", "--simple-output", "--CI"],
                work, work / "mutation.log",
            )
            assert (work / "app.py").read_text() == original, "Mutmut did not restore source"
            command([str(Path(sys.executable).parent / "mutmut"), "results"],
                    work, work / "mutants.txt")
            command([str(Path(sys.executable).parent / "mutmut"), "junitxml",
                     "--suspicious-policy=error", "--untested-policy=error"],
                    work, work / "mutants.xml")
            command([str(Path(sys.executable).parent / "mutmut"), "show", "all"],
                    work, work / "mutants.diff")
            import xml.etree.ElementTree as ET
            cases = ET.parse(work / "mutants.xml").getroot().findall(".//testcase")
            counts = {"killed": 0, "survived": 0, "other": 0}
            for case in cases:
                if case.find("failure") is not None:
                    counts["survived"] += 1
                elif case.find("error") is not None or case.find("skipped") is not None:
                    counts["other"] += 1
                else:
                    counts["killed"] += 1
            outcomes = []
            try:
                for fault in faults[project]:
                    changed = original
                    for before, after in fault["replacements"]:
                        assert before in changed, fault["id"]
                        changed = changed.replace(before, after)
                    (work / "app.py").write_text(changed)
                    _, status = command(
                        [sys.executable, "-m", "pytest", "-x", "-q"],
                        work, work / f"fault-{fault['id']}.log", acceptable=(0, 1),
                    )
                    outcomes.append({"id": fault["id"], "detected": status == 1})
            finally:
                (work / "app.py").write_text(original)
            row = {
                "project": project, "suite": suite,
                "test_count": int(collected.group(1)),
                "input_sha256": {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in source.iterdir() if path.is_file()
                },
                "ordinary_seconds": timings,
                "ordinary_median_seconds": statistics.median(timings),
                "mutation_seconds": mutation_seconds,
                "coverage": cov, "mutants": counts, "faults": outcomes,
            }
            data["results"].append(row)
            (output / "results.json").write_text(json.dumps(data, indent=2) + "\n")
            print(json.dumps({k: row[k] for k in ("project", "suite", "mutants",
                                                "mutation_seconds")}), flush=True)
    return data


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--suites", nargs="+", default=["baseline", "control", "guided"],
                        choices=["baseline", "control", "guided", "score_only"])
    parser.add_argument("--projects", nargs="+", default=["checkout", "access"],
                        choices=["checkout", "access"])
    options = parser.parse_args()
    run(options.output.resolve(), options.suites, options.projects)
