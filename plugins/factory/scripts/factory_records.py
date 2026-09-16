"""Data-only loaders and validators for the records factory stages hand to each other.

Both the runner (through `runner/records.py`) and the shipped skill scripts import
this file, so a record means the same thing on both sides. Nothing here executes
record content: JSON is parsed strictly, sizes are checked before reading, and
every failure is a `RecordError` with a stable code and a bounded message.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

__all__ = [
    "RecordError", "E2E_SCHEMA", "E2E_MAX_BYTES", "LEGACY_HTML_MAX_BYTES", "EVIDENCE_FILE_MAX_BYTES",
    "load_json_bytes", "load_json_file", "sha256_file", "sha256_bytes", "validate_e2e", "load_e2e",
    "e2e_counts", "verify_e2e_evidence", "legacy_e2e_from_html", "decode_data_uri", "safe_relative", "bounded",
    "CONTRACT_SCHEMA", "CONTRACT_PATH", "BOUNDARIES", "validate_command", "validate_contract", "load_contract",
    "contract_gaps",
    "RESULT_SCHEMA", "RESULT_STATUSES", "CONDITION_CODES", "validate_result", "load_result",
    "INTENT_SCHEMA", "parse_scenarios", "parse_non_goals", "parse_decisions", "normalize_text", "canonical_sha256",
    "SCENARIO_MAP_SCHEMA", "TEST_RESULTS_SCHEMA", "validate_scenario_map", "load_scenario_map", "parse_test_results",
    "test_path", "REVIEW_SCHEMA", "GAUNTLET_SCHEMA", "GAUNTLET_CHECKS", "SPEC_REVIEW_LENSES", "load_review",
    "load_gauntlet",
]

E2E_SCHEMA = "factory.e2e/1"
E2E_MAX_BYTES = 8 * 1024 * 1024
LEGACY_HTML_MAX_BYTES = 64 * 1024 * 1024
EVIDENCE_FILE_MAX_BYTES = 16 * 1024 * 1024
MESSAGE_LIMIT = 400
ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
CONTRACT_SCHEMA = "factory.repo-contract/1"
CONTRACT_PATH = ".factory/contract.json"
CONTRACT_MAX_BYTES = 256 * 1024
BOUNDARIES = ("host", "workspace-write")
MAX_COMMAND_TIMEOUT_S = 6 * 60 * 60
LEGACY_BLOCK = re.compile(r"E2E_DATA_START.*?const\s+E2E_DATA\s*=\s*(.*?)\s*;\s*/\*\s*E2E_DATA_END", re.S)


class RecordError(Exception):
    """A record that cannot be used. `code` is stable; the message is bounded and actionable."""

    def __init__(self, code: str, message: str):
        super().__init__(bounded(message))
        self.code = code


def bounded(text: str, limit: int = MESSAGE_LIMIT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- strict JSON ----------------------------------------------------------------

def _no_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise RecordError("record.duplicate_key", f"duplicate key {key!r}")
        result[key] = value
    return result


def _no_constants(name: str) -> object:
    raise RecordError("record.non_finite", f"non-finite number {name} is not JSON")


def load_json_bytes(raw: bytes, *, name: str = "record", schema: str | None = None) -> dict:
    """Parse one JSON object: strict UTF-8, no duplicate keys, no NaN/Infinity, optional schema tag."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecordError("record.encoding", f"{name} is not UTF-8: {error}") from error
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_no_constants)
    except RecordError as error:
        raise RecordError(error.code, f"{name}: {error}") from error
    except (json.JSONDecodeError, RecursionError) as error:
        raise RecordError("record.invalid_json", f"{name} is not valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise RecordError("record.not_object", f"{name} must be a JSON object, got {type(data).__name__}")
    if schema is not None:
        found = data.get("schema")
        if found != schema:
            family = schema.rsplit("/", 1)[0]
            if isinstance(found, str) and found.startswith(family + "/"):
                raise RecordError("record.unsupported_version",
                                  f"{name} has schema {found!r}; this factory reads {schema!r}")
            raise RecordError("record.schema", f"{name} must declare \"schema\": {schema!r}, got {found!r}")
    return data


def load_json_file(path: Path, *, max_bytes: int, schema: str | None = None) -> dict:
    """Load a JSON record after checking its size, never reading more than max_bytes + 1."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError as error:
        raise RecordError("record.missing", f"{path} does not exist") from error
    except OSError as error:
        raise RecordError("record.unreadable", f"{path}: {error}") from error
    if size > max_bytes:
        raise RecordError("record.too_large", f"{path.name} is {size} bytes; the limit is {max_bytes}")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise RecordError("record.too_large", f"{path.name} grew past the {max_bytes} byte limit while reading")
    return load_json_bytes(raw, name=path.name, schema=schema)


def sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative(value: object, where: str) -> str:
    """A relative POSIX path that stays inside its base directory."""
    if not isinstance(value, str) or not value:
        raise RecordError("record.invalid", f"{where} must be a non-empty relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value:
        raise RecordError("record.invalid", f"{where} must stay inside its directory, got {value!r}")
    return value


# --- shape checks -----------------------------------------------------------------

class _Checker:
    def __init__(self, name: str):
        self.name = name
        self.problems: list[str] = []

    def add(self, where: str, message: str) -> None:
        if len(self.problems) < 20:
            self.problems.append(f"{where}: {message}")

    def keys(self, obj: dict, where: str, required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
        for key in required:
            if key not in obj:
                self.add(where, f"missing {key!r}")
        unknown = sorted(set(obj) - set(required) - set(optional))
        if unknown:
            self.add(where, f"unknown key(s) {', '.join(map(repr, unknown))}")

    def string(self, obj: dict, key: str, where: str, *, empty: bool = True) -> None:
        if key in obj and (not isinstance(obj[key], str) or (not empty and not obj[key].strip())):
            self.add(f"{where}.{key}", "must be a non-empty string" if not empty else "must be a string")

    def raise_if_any(self) -> None:
        if self.problems:
            raise RecordError("record.invalid", f"{self.name} is malformed: " + "; ".join(self.problems))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- e2e results --------------------------------------------------------------------

def validate_e2e(data: dict, *, name: str = "e2e record", legacy: bool = False) -> dict:
    """Check a factory.e2e/1 record's shape. Counts are derived, never declared."""
    check = _Checker(name)
    check.keys(data, "record", ("schema", "plan", "kind", "scenarios"), ("title", "generatedAt", "revision"))
    check.string(data, "plan", "record", empty=False)
    check.string(data, "title", "record")
    check.string(data, "generatedAt", "record")
    check.string(data, "revision", "record")
    if data.get("kind") not in ("frontend", "non-frontend"):
        check.add("record.kind", "must be 'frontend' or 'non-frontend'")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list):
        check.add("record.scenarios", "must be a list")
        scenarios = []
    if len(scenarios) > 500:
        check.add("record.scenarios", f"{len(scenarios)} scenarios exceed the limit of 500")
    seen: set[str] = set()
    for index, scenario in enumerate(scenarios[:500]):
        where = f"scenarios[{index}]"
        if not isinstance(scenario, dict):
            check.add(where, "must be an object")
            continue
        check.keys(scenario, where, ("id", "title", "status"),
                   ("given", "when", "then", "screenshots", "states", "assertions", "output", "durationMs"))
        scenario_id = scenario.get("id")
        if not isinstance(scenario_id, str) or not ID.match(scenario_id):
            check.add(f"{where}.id", "must be 1-64 letters, digits, '.', '_' or '-'")
        elif scenario_id in seen:
            check.add(f"{where}.id", f"duplicate scenario id {scenario_id!r}")
        else:
            seen.add(scenario_id)
        check.string(scenario, "title", where, empty=False)
        for key in ("given", "when", "then", "output"):
            check.string(scenario, key, where)
        if scenario.get("status") not in ("pass", "fail"):
            check.add(f"{where}.status", "must be 'pass' or 'fail'")
        if "durationMs" in scenario and (not _is_int(scenario["durationMs"]) or scenario["durationMs"] < 0):
            check.add(f"{where}.durationMs", "must be a non-negative integer")
        for shot_index, shot in enumerate(_list(check, scenario, "screenshots", where)):
            shot_where = f"{where}.screenshots[{shot_index}]"
            if not isinstance(shot, dict):
                check.add(shot_where, "must be an object")
                continue
            if legacy:
                check.keys(shot, shot_where, ("step", "dataUri"), ("caption",))
                if isinstance(shot.get("dataUri"), str) and not shot["dataUri"].startswith("data:image/"):
                    check.add(f"{shot_where}.dataUri", "must be a data:image/ URI")
            else:
                check.keys(shot, shot_where, ("step", "file", "sha256"), ("caption",))
                try:
                    safe_relative(shot.get("file"), f"{shot_where}.file")
                except RecordError as error:
                    check.add(f"{shot_where}.file", str(error))
                if not isinstance(shot.get("sha256"), str) or not SHA256.match(shot["sha256"]):
                    check.add(f"{shot_where}.sha256", "must be a lowercase hex SHA-256")
            check.string(shot, "step", shot_where, empty=False)
            check.string(shot, "caption", shot_where)
        for state_index, state in enumerate(_list(check, scenario, "states", where)):
            state_where = f"{where}.states[{state_index}]"
            if not isinstance(state, dict):
                check.add(state_where, "must be an object")
                continue
            check.keys(state, state_where, ("step", "entity", "before", "after"), ("caption",))
            for key in ("step", "entity"):
                check.string(state, key, state_where, empty=False)
            check.string(state, "caption", state_where)
        for assertion_index, assertion in enumerate(_list(check, scenario, "assertions", where)):
            assertion_where = f"{where}.assertions[{assertion_index}]"
            if not isinstance(assertion, dict):
                check.add(assertion_where, "must be an object")
                continue
            check.keys(assertion, assertion_where, ("name", "passed"), ("detail",))
            check.string(assertion, "name", assertion_where, empty=False)
            check.string(assertion, "detail", assertion_where)
            if not isinstance(assertion.get("passed"), bool):
                check.add(f"{assertion_where}.passed", "must be true or false")
    check.raise_if_any()
    return data


def _list(check: _Checker, obj: dict, key: str, where: str) -> list:
    value = obj.get(key, [])
    if not isinstance(value, list):
        check.add(f"{where}.{key}", "must be a list")
        return []
    return value


def load_e2e(path: Path) -> dict:
    """Load and validate a factory.e2e/1 sidecar without touching its evidence files."""
    return validate_e2e(load_json_file(path, max_bytes=E2E_MAX_BYTES, schema=E2E_SCHEMA), name=Path(path).name)


def e2e_counts(record: dict) -> dict:
    """Totals derived from the scenario records; a scenario with a failed assertion counts as failed."""
    passed = failed = 0
    for scenario in record["scenarios"]:
        assertions_ok = all(a.get("passed") for a in scenario.get("assertions", []))
        if scenario["status"] == "pass" and assertions_ok:
            passed += 1
        else:
            failed += 1
    return {"total": passed + failed, "passed": passed, "failed": failed}


def verify_e2e_evidence(record: dict, base_dir: Path) -> list[str]:
    """Problems with the evidence files a record references: missing, escaping, oversized, or altered."""
    problems: list[str] = []
    base = Path(base_dir).resolve()
    for scenario in record["scenarios"]:
        for shot in scenario.get("screenshots", []):
            if "file" not in shot:
                continue
            target = (base / shot["file"]).resolve()
            try:
                target.relative_to(base)
            except ValueError:
                problems.append(f"{scenario['id']}: {shot['file']} resolves outside {base}")
                continue
            if not target.is_file():
                problems.append(f"{scenario['id']}: evidence file {shot['file']} does not exist")
                continue
            if target.stat().st_size > EVIDENCE_FILE_MAX_BYTES:
                problems.append(f"{scenario['id']}: evidence file {shot['file']} exceeds {EVIDENCE_FILE_MAX_BYTES} bytes")
                continue
            if sha256_file(target) != shot["sha256"]:
                problems.append(f"{scenario['id']}: evidence file {shot['file']} does not match its sha256")
    return problems


# --- legacy reports ---------------------------------------------------------------------

def legacy_e2e_from_html(path: Path) -> dict:
    """Read an old HTML report's E2E_DATA block as data, for inspection and evidence extraction only.

    The block is accepted only when it is strict JSON; a JavaScript literal (bare keys,
    comments, expressions) is never evaluated or rewritten and needs regenerated evidence.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError as error:
        raise RecordError("record.missing", f"{path}: {error}") from error
    if size > LEGACY_HTML_MAX_BYTES:
        raise RecordError("record.too_large", f"{path.name} is {size} bytes; the legacy limit is {LEGACY_HTML_MAX_BYTES}")
    text = path.read_bytes()[: LEGACY_HTML_MAX_BYTES + 1].decode("utf-8", errors="replace")
    match = LEGACY_BLOCK.search(text)
    if not match:
        raise RecordError("record.legacy_missing", f"{path.name} has no E2E_DATA block between its markers")
    try:
        old = load_json_bytes(match.group(1).encode("utf-8"), name=f"{path.name} E2E_DATA")
    except RecordError as error:
        raise RecordError("record.legacy_literal",
                          f"{error}; legacy reports are read only as strict JSON, so regenerate the e2e evidence "
                          "as a factory.e2e/1 sidecar") from error
    scenarios = []
    for index, scenario in enumerate(old.get("scenarios") or [], 1):
        if not isinstance(scenario, dict):
            raise RecordError("record.invalid", f"{path.name} scenario {index} is not an object")
        converted = {
            "id": str(scenario.get("id") or f"scenario-{index}"),
            "title": str(scenario.get("title") or scenario.get("id") or f"scenario {index}"),
            "status": scenario.get("status"),
        }
        for key in ("given", "when", "then"):
            if isinstance(scenario.get(key), str):
                converted[key] = scenario[key]
        if isinstance(scenario.get("logsOrOutput"), str) and scenario["logsOrOutput"]:
            converted["output"] = scenario["logsOrOutput"]
        if _is_int(scenario.get("durationMs")):
            converted["durationMs"] = scenario["durationMs"]
        converted["screenshots"] = [
            {k: v for k, v in shot.items() if k in ("step", "caption", "dataUri")}
            for shot in scenario.get("screenshots") or [] if isinstance(shot, dict)
        ]
        converted["states"] = [
            {k: v for k, v in state.items() if k in ("step", "caption", "entity", "before", "after")}
            for state in scenario.get("dataModelState") or [] if isinstance(state, dict)
        ]
        scenarios.append(converted)
    record = {"schema": E2E_SCHEMA, "plan": str(old.get("planName") or "plan"), "kind": old.get("kind", "non-frontend"),
              "scenarios": scenarios}
    for key in ("title", "generatedAt"):
        if isinstance(old.get(key), str):
            record[key] = old[key]
    return validate_e2e(record, name=f"{path.name} (legacy)", legacy=True)


def decode_data_uri(uri: str, limit: int = EVIDENCE_FILE_MAX_BYTES) -> tuple[str, bytes]:
    """(extension, bytes) of a base64 image data URI, bounded before decoding."""
    match = re.match(r"^data:image/([a-z0-9.+-]+);base64,", uri)
    if not match:
        raise RecordError("record.invalid", "screenshot is not a base64 data:image/ URI")
    payload = uri[match.end():]
    if len(payload) * 3 // 4 > limit:
        raise RecordError("record.too_large", f"screenshot data URI exceeds {limit} bytes")
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, base64.binascii.Error) as error:
        raise RecordError("record.invalid", f"screenshot data URI is not valid base64: {error}") from error
    extension = {"jpeg": "jpg", "svg+xml": "svg"}.get(match.group(1), match.group(1))
    return extension, raw


# --- repository execution contract --------------------------------------------------------

def validate_command(command: object, where: str, check: "_Checker", *, placeholders: tuple[str, ...] = ()) -> None:
    """One command record: an argv (`run`) or one repository script (`script`), never a shell string."""
    if not isinstance(command, dict):
        check.add(where, "must be an object")
        return
    check.keys(command, where, ("id",), ("run", "script", "args", "shell", "cwd", "env", "set", "timeout_s",
                                         "boundary", "description", "ready"))
    if "ready" in command:
        validate_command({"id": "ready", **command["ready"]} if isinstance(command["ready"], dict) else command["ready"],
                         f"{where}.ready", check, placeholders=placeholders)
    if not isinstance(command.get("id"), str) or not ID.match(command["id"]):
        check.add(f"{where}.id", "must be 1-64 letters, digits, '.', '_' or '-'")
    has_run, has_script = "run" in command, "script" in command
    if has_run == has_script:
        check.add(where, "needs exactly one of 'run' (an argv list) or 'script' (a repository path)")
    if has_run:
        run = command["run"]
        if not isinstance(run, list) or not run or not all(isinstance(part, str) and part for part in run):
            check.add(f"{where}.run", "must be a non-empty list of non-empty strings (an argv, not a shell line)")
        for key in ("args", "shell"):
            if key in command:
                check.add(f"{where}.{key}", "only applies to a 'script' command")
    if has_script:
        try:
            safe_relative(command["script"], f"{where}.script")
        except RecordError as error:
            check.add(f"{where}.script", str(error))
        if "args" in command and (not isinstance(command["args"], list)
                                  or not all(isinstance(part, str) for part in command["args"])):
            check.add(f"{where}.args", "must be a list of strings")
        if command.get("shell", "sh") not in ("sh", "bash"):
            check.add(f"{where}.shell", "must be 'sh' or 'bash'")
    parts = [part for key in ("run", "args") if isinstance(command.get(key), list) for part in command[key]]
    for part in parts:
        for token in re.findall(r"\{([a-z_]+)\}", part if isinstance(part, str) else ""):
            if token not in placeholders:
                check.add(where, f"unknown placeholder {{{token}}}")
    if "cwd" in command:
        try:
            safe_relative(command["cwd"], f"{where}.cwd")
        except RecordError as error:
            check.add(f"{where}.cwd", str(error))
    env = command.get("env", [])
    if not isinstance(env, list) or not all(isinstance(name, str) and ENV_NAME.match(name) for name in env):
        check.add(f"{where}.env", "must be a list of environment variable names")
    values = command.get("set", {})
    if not isinstance(values, dict) or not all(ENV_NAME.match(k) and isinstance(v, str) for k, v in values.items()):
        check.add(f"{where}.set", "must map environment variable names to strings")
    if "timeout_s" in command:
        timeout = command["timeout_s"]
        if not _is_int(timeout) or not 0 < timeout <= MAX_COMMAND_TIMEOUT_S:
            check.add(f"{where}.timeout_s", f"must be an integer from 1 to {MAX_COMMAND_TIMEOUT_S}")
    if "boundary" in command and command["boundary"] not in BOUNDARIES:
        check.add(f"{where}.boundary", f"must be one of {', '.join(BOUNDARIES)}")
    check.string(command, "description", where)


def _commands(check: "_Checker", data: dict, key: str, *, required: bool, placeholders: tuple[str, ...] = ()) -> None:
    commands = data.get(key, [])
    if not isinstance(commands, list):
        check.add(key, "must be a list of command records")
        return
    if required and not commands:
        check.add(key, "must list at least one command")
    seen: set[str] = set()
    for index, command in enumerate(commands):
        validate_command(command, f"{key}[{index}]", check, placeholders=placeholders)
        if isinstance(command, dict) and isinstance(command.get("id"), str):
            if command["id"] in seen:
                check.add(f"{key}[{index}].id", f"duplicate command id {command['id']!r}")
            seen.add(command["id"])


PORT_NAME = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
CONTRACT_PLACEHOLDERS = ("out", "tests", "results", "junit", "run_dir")


def validate_contract(data: dict, *, name: str = CONTRACT_PATH) -> dict:
    """A factory.repo-contract/1 record: how the factory sets up, checks, launches, and observes this repository.

    `ci` is required: either the pull-request checks that must pass, or an explicit, reasoned
    statement that the repository has none. `e2e` is either a driver or a reasoned `none`.
    """
    check = _Checker(name)
    check.keys(data, "contract", ("schema", "validation", "ci"),
               ("boundary", "environment", "setup", "tests", "e2e", "services", "ports", "gauntlet", "description"))
    if "boundary" in data and data["boundary"] not in BOUNDARIES:
        check.add("contract.boundary", f"must be one of {', '.join(BOUNDARIES)}")
    check.string(data, "description", "contract")
    environment = data.get("environment", {})
    if not isinstance(environment, dict):
        check.add("environment", "must be an object")
    else:
        check.keys(environment, "environment", (), ("required", "passthrough"))
        for key in ("required", "passthrough"):
            names = environment.get(key, [])
            if not isinstance(names, list) or not all(isinstance(n, str) and ENV_NAME.match(n) for n in names):
                check.add(f"environment.{key}", "must be a list of environment variable names")
    ports = data.get("ports", [])
    if not isinstance(ports, list) or not all(isinstance(p, str) and PORT_NAME.match(p) for p in ports):
        check.add("ports", "must be a list of lowercase port names")
        ports = []
    placeholders = CONTRACT_PLACEHOLDERS + tuple(f"port_{name}" for name in ports)
    _commands(check, data, "setup", required=False, placeholders=placeholders)
    _commands(check, data, "validation", required=True, placeholders=placeholders)
    services = data.get("services", [])
    _commands(check, data, "services", required=False, placeholders=placeholders)
    for index, service in enumerate(services if isinstance(services, list) else []):
        if isinstance(service, dict) and "ready" not in service:
            check.add(f"services[{index}]", "needs a 'ready' command that exits 0 once the service accepts work")
    tests = data.get("tests")
    if tests is not None:
        if not isinstance(tests, dict):
            check.add("tests", "must be an object")
        else:
            check.keys(tests, "tests", ("run", "results"), ("layers",))
            validate_command({"id": "tests", **tests["run"]} if isinstance(tests.get("run"), dict) else tests.get("run"),
                             "tests.run", check, placeholders=placeholders)
            if tests.get("results") not in ("junit", "factory.test-results/1"):
                check.add("tests.results", "must be 'junit' or 'factory.test-results/1'")
            layers = tests.get("layers", {})
            if not isinstance(layers, dict) or not all(
                    k in ("unit", "integration") and isinstance(v, list) and all(isinstance(g, str) for g in v)
                    for k, v in layers.items()):
                check.add("tests.layers", "must map unit/integration to lists of path globs")
    e2e = data.get("e2e")
    if e2e is not None:
        if not isinstance(e2e, dict):
            check.add("e2e", "must be an object")
        elif "none" in e2e:
            check.keys(e2e, "e2e", ("none",))
            check.string(e2e, "none", "e2e", empty=False)
        else:
            check.keys(e2e, "e2e", ("driver",), ("services",))
            driver = e2e.get("driver")
            validate_command({"id": "e2e", **driver} if isinstance(driver, dict) else driver, "e2e.driver", check,
                             placeholders=placeholders)
            service_ids = {s.get("id") for s in services if isinstance(s, dict)} if isinstance(services, list) else set()
            for sid in e2e.get("services", []) if isinstance(e2e.get("services", []), list) else ["?"]:
                if sid not in service_ids:
                    check.add("e2e.services", f"names unknown service {sid!r}")
    ci = data.get("ci")
    if not isinstance(ci, dict):
        check.add("ci", "must be an object: {\"required_checks\": [...]} or {\"none\": \"why\"}")
    elif "none" in ci:
        check.keys(ci, "ci", ("none",))
        check.string(ci, "none", "ci", empty=False)
    else:
        check.keys(ci, "ci", ("required_checks",), ("allow_skipped",))
        names = ci.get("required_checks")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n.strip() for n in names):
            check.add("ci.required_checks", "must list at least one check name, or use {\"none\": \"why\"}")
        allowed = ci.get("allow_skipped", [])
        if not isinstance(allowed, list) or not all(isinstance(n, str) for n in allowed):
            check.add("ci.allow_skipped", "must be a list of check names")
    gauntlet = data.get("gauntlet", {})
    if not isinstance(gauntlet, dict):
        check.add("gauntlet", "must be an object")
        gauntlet = {}
    for check_id, entry in gauntlet.items():
        where = f"gauntlet.{check_id}"
        if check_id not in ("static-analysis", "security", "dead-code", "duplication", "dependency-rules",
                            "complexity-coverage", "flakiness", "mutation"):
            check.add(where, "is not a gauntlet check")
            continue
        if not isinstance(entry, dict) or len(entry) != 1 or not ("command" in entry or "not_applicable" in entry):
            check.add(where, "must be {\"command\": {...}} to pin the command or {\"not_applicable\": \"why\"}")
        elif "command" in entry:
            validate_command({"id": check_id, **entry["command"]} if isinstance(entry["command"], dict)
                             else entry["command"], f"{where}.command", check, placeholders=placeholders)
        elif not isinstance(entry["not_applicable"], str) or not entry["not_applicable"].strip():
            check.add(f"{where}.not_applicable", "must give the repository-grounded reason")
    check.raise_if_any()
    return data


def contract_gaps(contract: dict, scenarios: list[dict]) -> list[str]:
    """What an approved plan needs from the contract that it does not declare; checked before a long build."""
    layers = {scenario["layer"] for scenario in scenarios}
    gaps = []
    if layers & {"unit", "integration"} and "tests" not in contract:
        gaps.append("the plan has unit or integration scenarios but the contract declares no 'tests' runner")
    if "e2e" in layers:
        e2e = contract.get("e2e")
        if e2e is None:
            gaps.append("the plan has [e2e] scenarios but the contract declares no 'e2e' driver")
        elif "none" in e2e:
            gaps.append(f"the plan has [e2e] scenarios but the contract says there is no e2e driver ({e2e['none']})")
    return gaps


def load_contract(path: Path) -> dict:
    return validate_contract(load_json_file(path, max_bytes=CONTRACT_MAX_BYTES, schema=CONTRACT_SCHEMA),
                             name=Path(path).name)


# --- stage results and typed conditions --------------------------------------------------

RESULT_SCHEMA = 2
RESULT_STATUSES = ("done", "blocked", "failed")
RESULT_MAX_BYTES = 256 * 1024

# The fixed park list (factory/references/factory-run.md). Retryability belongs to the code,
# never to the stage that reports it.
CONDITION_CODES: dict[str, dict] = {
    "premise.invalidated": {"category": "premise", "retryable": False,
                            "next": "decide the premise, then `factory retry {run} --note \"...\"` or reopen scope"},
    "scope.new_effort": {"category": "premise", "retryable": False,
                         "next": "scope the new effort separately, then `factory retry {run} --note \"...\"`"},
    "secret.found": {"category": "security", "retryable": False,
                     "next": "remove and rotate the secret, then `factory retry {run}`"},
    "environment.missing_credentials": {"category": "environment", "retryable": False,
                                        "next": "provide the credentials to the runner's environment, then "
                                                "`factory retry {run}`"},
    "action.destructive": {"category": "destructive", "retryable": False,
                           "next": "perform or refuse the action yourself, then `factory retry {run} --note \"...\"`"},
    "launch.unavailable": {"category": "environment", "retryable": False,
                           "next": "record a safe launch command for e2e, then `factory retry {run}`"},
    "input.unusable": {"category": "input", "retryable": False,
                       "next": "repair the handed-off input (spec, diff, or tooling), then `factory retry {run}`"},
    "decision.verification_failed": {"category": "decision", "retryable": True,
                                     "next": "the runner retries with the verification failure as its reason"},
}
CONDITION_ID = re.compile(r"^C[1-9][0-9]{0,3}$")


def validate_result(data: dict, *, stage: str, name: str = "result") -> dict:
    """A schema-2 stage result. Park conditions are typed; free-form human calls and retryable flags are gone."""
    check = _Checker(name)
    if data.get("schema") != RESULT_SCHEMA:
        found = data.get("schema")
        detail = ("schema 1 results are no longer read: park conditions are typed `conditions`"
                  if found == 1 else f"schema must be {RESULT_SCHEMA}, got {found!r}")
        raise RecordError("record.unsupported_version", f"{name}: {detail}")
    check.keys(data, "result", ("schema", "stage", "status", "reason", "conditions"),
               ("artifacts", "next", "counts", "pr_url", "draft"))
    if data.get("stage") != stage:
        check.add("result.stage", f"must be {stage!r}")
    if data.get("status") not in RESULT_STATUSES:
        check.add("result.status", f"must be one of {', '.join(RESULT_STATUSES)}")
    check.string(data, "reason", "result")
    if "reason" in data and not isinstance(data["reason"], str):
        check.add("result.reason", "must be a string")
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or not all(isinstance(a, str) for a in artifacts):
        check.add("result.artifacts", "must be a list of paths")
    if "next" in data and data["next"] is not None and not isinstance(data["next"], str):
        check.add("result.next", "must be a stage name or null")
    counts = data.get("counts", {})
    if not isinstance(counts, dict) or not all(_is_int(v) and v >= 0 for v in counts.values()):
        check.add("result.counts", "must map names to non-negative integers")
    if "pr_url" in data and data["pr_url"] is not None and not isinstance(data["pr_url"], str):
        check.add("result.pr_url", "must be a string or null")
    if "draft" in data and not isinstance(data["draft"], bool):
        check.add("result.draft", "must be true or false")
    conditions = data.get("conditions")
    if not isinstance(conditions, list):
        check.add("result.conditions", "must be a list (empty when nothing needs a human)")
        conditions = []
    unresolved = 0
    for index, condition in enumerate(conditions[:50]):
        where = f"conditions[{index}]"
        if not isinstance(condition, dict):
            check.add(where, "must be an object")
            continue
        check.keys(condition, where, ("code", "summary"), ("evidence", "requires_env", "resolution", "resolves"))
        if condition.get("code") not in CONDITION_CODES:
            check.add(f"{where}.code", f"unknown condition code {condition.get('code')!r}; use one of "
                                       f"{', '.join(sorted(CONDITION_CODES))}")
        check.string(condition, "summary", where, empty=False)
        evidence = condition.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(e, str) and e.strip() for e in evidence):
            check.add(f"{where}.evidence", "must be a list of non-empty strings")
        names = condition.get("requires_env", [])
        if not isinstance(names, list) or not all(isinstance(n, str) and ENV_NAME.match(n) for n in names):
            check.add(f"{where}.requires_env", "must be a list of environment variable names")
        resolution = condition.get("resolution", "unresolved")
        if resolution not in ("unresolved", "resolved"):
            check.add(f"{where}.resolution", "must be 'unresolved' or 'resolved'")
        if resolution == "resolved":
            if not isinstance(condition.get("resolves"), str) or not CONDITION_ID.match(condition["resolves"]):
                check.add(f"{where}.resolves", "a resolved condition names the runner's condition id, such as 'C1'")
            if not evidence:
                check.add(f"{where}.evidence", "a resolution needs evidence")
        else:
            unresolved += 1
            if "resolves" in condition:
                check.add(f"{where}.resolves", "only a resolved condition names what it resolves")
    if len(conditions) > 50:
        check.add("result.conditions", "more than 50 conditions")
    if data.get("status") == "done" and unresolved:
        check.add("result.status", "cannot be 'done' while a condition is unresolved")
    check.raise_if_any()
    return data


def load_result(path: Path, *, stage: str) -> dict:
    data = load_json_file(path, max_bytes=RESULT_MAX_BYTES)
    return validate_result(data, stage=stage, name=Path(path).name)


# --- approved intent -------------------------------------------------------------------------

INTENT_SCHEMA = "factory.intent/1"
_HEADING = re.compile(r"^##\s+(.*?)\s*$")
_CHANGE_SET = re.compile(r"^\s*(\d+)\.\s+\S")
_TESTS = re.compile(r"^\s*tests:\s*(.*)$", re.I)
_SCENARIO = re.compile(r"^\[(unit|integration|e2e)\]\s*((?:\[repro\]\s*)?)(.+)$", re.S)
_DECISION = re.compile(r"^(D-[a-z0-9]+(?:-[a-z0-9]+)*):")


def normalize_text(text: str) -> str:
    return " ".join(text.split())


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                          .encode("utf-8")).hexdigest()


def _section_lines(spec_text: str, name: str) -> list[str]:
    lines: list[str] = []
    inside = False
    for line in spec_text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            inside = heading.group(1).strip().lower() == name
            continue
        if inside:
            lines.append(line)
    return lines


def parse_scenarios(spec_text: str) -> list[dict]:
    """Every layer-tagged scenario of the change plan, in order: change set, layer, requirement, repro flag.

    A bug-fix scenario whose test must fail on the base and pass on the change is tagged
    `[repro]` right after its layer: `[unit] [repro] duplicate delivery -> processed once`.
    """
    scenarios: list[dict] = []
    change_set = None
    for line in _section_lines(spec_text, "change plan"):
        match = _CHANGE_SET.match(line)
        if match:
            change_set = int(match.group(1))
            continue
        tests = _TESTS.match(line)
        if not tests or change_set is None or tests.group(1).strip().lower().startswith("none"):
            continue
        for chunk in tests.group(1).split(";"):
            scenario = _SCENARIO.match(chunk.strip())
            if scenario:
                scenarios.append({"change_set": change_set, "layer": scenario.group(1),
                                  "requirement": normalize_text(scenario.group(3)),
                                  "repro": bool(scenario.group(2).strip())})
    return scenarios


def parse_non_goals(spec_text: str) -> list[str]:
    """The scope section's considered non-goals: its `⊘` lines."""
    return [normalize_text(line.strip()[1:]) for line in _section_lines(spec_text, "scope")
            if line.strip().startswith("\u2298")]


def parse_decisions(spec_text: str) -> dict[str, str]:
    """Each research decision's full entry text by slug, for decision deltas."""
    decisions: dict[str, list[str]] = {}
    current = None
    for line in _section_lines(spec_text, "research"):
        match = _DECISION.match(line)
        if match:
            current = match.group(1)
            decisions[current] = [line.rstrip()]
        elif current is not None:
            if line.strip():
                decisions[current].append(line.rstrip())
            else:
                current = None
    return {slug: "\n".join(lines) for slug, lines in decisions.items()}


# --- scenario evidence --------------------------------------------------------------------------

SCENARIO_MAP_SCHEMA = "factory.scenario-map/1"
TEST_RESULTS_SCHEMA = "factory.test-results/1"
RESULTS_MAX_BYTES = 32 * 1024 * 1024
CASE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
TEST_ID = re.compile(r"^\S(?:.{0,510}\S)?$")
OUTCOMES = ("passed", "failed", "error", "skipped")


def test_path(test_id: str) -> str | None:
    """The file part of a `path::name` test id, when it has one."""
    return test_id.split("::", 1)[0] if "::" in test_id else None


def validate_scenario_map(data: dict, scenarios: list[dict], *, name: str = "scenario-map.json") -> dict:
    """How each present scenario is proven: unit and integration by test ids, e2e by a driver case.

    `scenarios` are the run's present scenarios ({id, layer}). Every one must be mapped at its
    own layer; ids the run does not know are rejected; a test listed twice for one scenario is
    rejected, so repetition cannot manufacture coverage.
    """
    check = _Checker(name)
    check.keys(data, "map", ("schema", "scenarios"))
    mapping = data.get("scenarios")
    if not isinstance(mapping, dict):
        check.add("map.scenarios", "must map scenario ids to their evidence")
        mapping = {}
    known = {s["id"]: s for s in scenarios}
    for scenario_id, entry in mapping.items():
        where = f"scenarios.{scenario_id}"
        if scenario_id not in known:
            check.add(where, "is not a scenario of this run (see scenarios_file in factory-run.json)")
            continue
        if not isinstance(entry, dict):
            check.add(where, "must be an object")
            continue
        layer = known[scenario_id]["layer"]
        if entry.get("layer") != layer:
            check.add(f"{where}.layer", f"must be {layer!r}, the layer the spec tags it with")
        if layer == "e2e":
            check.keys(entry, where, ("layer", "e2e_case"))
            if not isinstance(entry.get("e2e_case"), str) or not CASE.match(entry["e2e_case"]):
                check.add(f"{where}.e2e_case", "must name the driver case (lowercase letters, digits, '.', '_', '-')")
        else:
            check.keys(entry, where, ("layer", "tests"))
            tests = entry.get("tests")
            if not isinstance(tests, list) or not tests:
                check.add(f"{where}.tests", "must list at least one test id")
                continue
            if not all(isinstance(test, str) and TEST_ID.match(test) for test in tests):
                check.add(f"{where}.tests", "must be non-empty test id strings")
            elif len(set(tests)) != len(tests):
                check.add(f"{where}.tests", "lists the same test more than once")
    for scenario_id in known:
        if scenario_id not in mapping:
            check.add(f"scenarios.{scenario_id}", f"is not mapped ([{known[scenario_id]['layer']}] "
                                                  f"{known[scenario_id].get('requirement', '')})")
    check.raise_if_any()
    return data


def load_scenario_map(path: Path, scenarios: list[dict]) -> dict:
    return validate_scenario_map(load_json_file(path, max_bytes=RESULT_MAX_BYTES, schema=SCENARIO_MAP_SCHEMA),
                                 scenarios, name=Path(path).name)


def parse_test_results(path: Path, fmt: str) -> dict[str, dict]:
    """Test outcomes by id from a runner-owned results file: JUnit XML or factory.test-results/1 JSON.

    A test id that appears twice is an error, because its outcome would be ambiguous.
    """
    path = Path(path)
    if fmt == TEST_RESULTS_SCHEMA:
        data = load_json_file(path, max_bytes=RESULTS_MAX_BYTES, schema=TEST_RESULTS_SCHEMA)
        check = _Checker(path.name)
        check.keys(data, "results", ("schema", "tests"), ("framework",))
        tests = data.get("tests")
        if not isinstance(tests, list):
            check.add("results.tests", "must be a list")
            tests = []
        entries = []
        for index, test in enumerate(tests):
            where = f"tests[{index}]"
            if not isinstance(test, dict):
                check.add(where, "must be an object")
                continue
            check.keys(test, where, ("id", "outcome"), ("detail", "duration_ms"))
            if not isinstance(test.get("id"), str) or not test["id"]:
                check.add(f"{where}.id", "must be a non-empty string")
            if test.get("outcome") not in OUTCOMES:
                check.add(f"{where}.outcome", f"must be one of {', '.join(OUTCOMES)}")
            entries.append(test)
        check.raise_if_any()
        pairs = [(test["id"], {"outcome": test["outcome"], "detail": test.get("detail", "")}) for test in entries]
    elif fmt == "junit":
        pairs = _junit(path)
    else:
        raise RecordError("record.invalid", f"unknown test results format {fmt!r}")
    results: dict[str, dict] = {}
    for test_id, outcome in pairs:
        if test_id in results:
            raise RecordError("record.duplicate_test", f"{path.name} reports test {test_id!r} more than once")
        results[test_id] = outcome
    return results


def _junit(path: Path) -> list[tuple[str, dict]]:
    import xml.etree.ElementTree as ElementTree
    try:
        size = path.stat().st_size
    except OSError as error:
        raise RecordError("record.missing", f"{path} does not exist") from error
    if size > RESULTS_MAX_BYTES:
        raise RecordError("record.too_large", f"{path.name} is {size} bytes; the limit is {RESULTS_MAX_BYTES}")
    raw = path.read_bytes()
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise RecordError("record.invalid", f"{path.name} declares a DTD or entities, which test results never need")
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        raise RecordError("record.invalid_xml", f"{path.name} is not valid XML: {error}") from error
    pairs = []
    for case in root.iter("testcase"):
        classname, name = case.get("classname") or "", case.get("name") or ""
        file = case.get("file")
        if "." in classname:
            module, _, klass = classname.rpartition(".")
            parts = module.split(".")
            if klass[:1].isupper():
                test_id = f"{'/'.join(parts)}.py::{klass}::{name}"
            else:
                test_id = f"{'/'.join(parts + [klass])}.py::{name}"
        else:
            test_id = f"{file or classname}::{name}"
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        detail = ""
        for tag in ("failure", "error", "skipped"):
            node = case.find(tag)
            if node is not None:
                detail = (node.get("message") or node.text or "").strip()[:500]
        pairs.append((test_id, {"outcome": outcome, "detail": detail}))
    return pairs


# --- review and gauntlet records ---------------------------------------------------------------

REVIEW_SCHEMA = "factory.review/1"
GAUNTLET_SCHEMA = "factory.gauntlet/1"
SPEC_REVIEW_LENSES = ("feasibility", "completeness", "consistency", "testability")
GAUNTLET_CHECKS = ("static-analysis", "security", "dead-code", "duplication", "dependency-rules",
                   "complexity-coverage", "flakiness", "mutation")


def load_review(path: Path, *, required_lenses: tuple[str, ...] = ()) -> dict:
    """A factory.review/1 record that is complete, verified, and whose verdict follows from its findings."""
    data = load_json_file(path, max_bytes=RESULT_MAX_BYTES * 8, schema=REVIEW_SCHEMA)
    check = _Checker(Path(path).name)
    for key in ("expected_lenses", "completed_lenses", "missing_lenses", "blockers", "concerns"):
        if not isinstance(data.get(key), list):
            check.add(key, "must be a list")
    verification = data.get("verification")
    if not isinstance(verification, dict) or not isinstance(verification.get("missing"), list):
        check.add("verification", "must record the findings still missing a verifier")
    check.raise_if_any()
    problems = []
    if data.get("completeness") != "complete":
        missing = ", ".join(data["missing_lenses"]) or "none"
        unverified = ", ".join(verification["missing"]) or "none"
        problems.append(f"the review is incomplete (missing lenses: {missing}; unverified findings: {unverified})")
    if not data["expected_lenses"]:
        problems.append("the review named no expected lenses")
    absent = sorted(set(required_lenses) - set(data["completed_lenses"]))
    if absent:
        problems.append(f"mandatory lenses without a valid result: {', '.join(absent)}")
    if data["missing_lenses"] or verification["missing"]:
        problems.append("a complete review cannot list missing lenses or unverified findings")
    derived = "BLOCK" if data["blockers"] else "CONCERNS" if data["concerns"] else "PASS"
    if data.get("completeness") == "complete" and data.get("verdict") != derived:
        problems.append(f"verdict {data.get('verdict')!r} does not follow from its findings ({derived})")
    if problems:
        raise RecordError("review.incomplete", f"{Path(path).name}: " + "; ".join(problems))
    return data


def load_gauntlet(path: Path) -> dict:
    """A factory.gauntlet/1 record naming every mandatory check exactly once."""
    data = load_json_file(path, max_bytes=RESULT_MAX_BYTES, schema=GAUNTLET_SCHEMA)
    check = _Checker(Path(path).name)
    check.keys(data, "gauntlet", ("schema", "revision", "checks"), ("scope",))
    check.string(data, "revision", "gauntlet", empty=False)
    checks = data.get("checks")
    if not isinstance(checks, list):
        check.add("gauntlet.checks", "must be a list")
        checks = []
    seen: list[str] = []
    for index, entry in enumerate(checks):
        where = f"checks[{index}]"
        if not isinstance(entry, dict):
            check.add(where, "must be an object")
            continue
        check.keys(entry, where, ("id", "applicable"),
                   ("command", "tool_version", "scope", "threshold_source", "surviving", "reason"))
        if entry.get("id") not in GAUNTLET_CHECKS:
            check.add(f"{where}.id", f"must be one of {', '.join(GAUNTLET_CHECKS)}")
            continue
        seen.append(entry["id"])
        if not isinstance(entry.get("applicable"), bool):
            check.add(f"{where}.applicable", "must be true or false")
        elif entry["applicable"]:
            validate_command({"id": entry["id"], **entry["command"]} if isinstance(entry.get("command"), dict)
                             else entry.get("command"), f"{where}.command", check)
            for key in ("tool_version", "scope", "threshold_source"):
                check.string(entry, key, where, empty=False)
                if key not in entry:
                    check.add(f"{where}.{key}", "is required for an applicable check")
            surviving = entry.get("surviving")
            if not isinstance(surviving, list) or not all(isinstance(s, str) for s in surviving):
                check.add(f"{where}.surviving", "must list surviving violations as strings (empty when clean)")
        else:
            check.string(entry, "reason", where, empty=False)
    for name in GAUNTLET_CHECKS:
        if seen.count(name) != 1:
            check.add("gauntlet.checks", f"{name} must appear exactly once (found {seen.count(name)})")
    check.raise_if_any()
    return data
