"""Tests for arch.py, the enforcement behind docs/architecture.toml.

Run from the repo root: python3 -m unittest discover -s dev/evals/tests -p 'test_arch.py' -t .
Each test builds a small repository in a scratch directory and runs the script as
a subprocess. The built-in checks, drift, decision links, and the ratchet need no
tools; the tests that run import-linter, ast-grep, dependency-cruiser, or
go-arch-lint are skipped when the tool is not on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "dev" / "scripts" / "arch.py"

PY_DECISIONS = """\
# Decisions

D-layering: Which way do dependencies point? (2026-11-24, user)
  ✓ down the layers - each layer is testable without the ones above
  ✗ anywhere - coupling
  enforced by: boundary:domain, boundary:telemetry, boundary:providers, boundary:service, boundary:ui, public:service

D-providers: How do cross-cutting concerns enter? (2026-11-24, user)
  ✓ through app.providers - one place to swap them
  ✗ anywhere - ad hoc coupling
  enforced by: providers

D-no-raw-subprocess: Who may spawn processes? (2026-11-24, user)
  ✓ nobody in product code - timeouts and shells need judgment
  ✗ anyone - each call site re-decides
  enforced by: no-raw-subprocess

D-deps: Which packages may Python code import? (2026-11-24, user)
  ✓ an allowlist - each dependency is a reviewed choice
  ✗ anything - supply-chain risk grows silently
  enforced by: dependencies:python
"""

PY_TOML = """\
version = 1
languages = ["python"]
layers = ["domain", "telemetry", "providers", "service", "ui"]

[settings.python]
root = "src"

[[modules]]
name = "domain"
layer = "domain"
path = "src/app/domain"
decision = "D-layering"
instead = "keep domain free of outward dependencies"

[[modules]]
name = "telemetry"
layer = "telemetry"
path = "src/app/telemetry"
decision = "D-layering"
instead = "telemetry depends on nothing"

[[modules]]
name = "providers"
layer = "providers"
path = "src/app/providers"
may_depend_on = ["telemetry"]
decision = "D-layering"
instead = "providers wraps cross-cutting concerns only"

[[modules]]
name = "service"
layer = "service"
path = "src/app/service"
may_depend_on = ["domain", "providers"]
public = ["src/app/service/__init__.py", "src/app/service/api.py"]
decision = "D-layering"
instead = "call app.service.api"

[[modules]]
name = "ui"
layer = "ui"
path = "src/app/ui"
may_depend_on = ["service"]
decision = "D-layering"
instead = "call app.service.api, never the domain"

[providers]
module = "providers"
concerns = ["telemetry"]
decision = "D-providers"
instead = "use app.providers.log"

[[banned]]
id = "no-raw-subprocess"
language = "python"
pattern = "subprocess.$METHOD($$$ARGS)"
message = "raw process spawning needs judgment about shells and timeouts"
instead = "call the framework's process runner"
decision = "D-no-raw-subprocess"
invalid = ["subprocess.run(['ls'])", "subprocess.Popen(cmd, shell=True)"]
valid = ["process.run(['ls'])", "my_subprocess.run(cmd)"]

[[dependencies]]
language = "python"
allow = ["requests"]
decision = "D-deps"
instead = "propose the package as a D- decision first"
"""

PY_FILES = {
    "src/app/__init__.py": "",
    "src/app/domain/__init__.py": "",
    "src/app/domain/model.py": "class Order:\n    pass\n",
    "src/app/telemetry/__init__.py": "",
    "src/app/telemetry/sink.py": "def emit(x):\n    pass\n",
    "src/app/providers/__init__.py": "from app.telemetry import sink\nlog = sink.emit\n",
    "src/app/service/__init__.py": "",
    "src/app/service/api.py": "import requests\nfrom app.domain.model import Order\nfrom app.providers import log\n",
    "src/app/service/_impl.py": "from app.domain import model\n",
    "src/app/ui/__init__.py": "",
    "src/app/ui/view.py": "from app.service import api\n",
}

NATIVE_DECISIONS = """\
D-layering: Which way do dependencies point? (2026-11-24, user)
  ✓ down - testable layers
  ✗ anywhere - coupling
  enforced by: boundary:core, boundary:net, boundary:app, public:net, boundary:domain, boundary:service, boundary:ui

D-crates: Which crates may Rust code use? (2026-11-24, user)
  ✓ an allowlist - reviewed additions
  ✗ any - supply-chain risk
  enforced by: dependencies:rust
"""

NATIVE_TOML = """\
version = 1
languages = ["cpp", "rust"]
layers = ["core", "net", "app"]

[settings.cpp]
include_roots = ["net/include"]

[[modules]]
name = "core"
layer = "core"
language = "cpp"
path = "core"
decision = "D-layering"
instead = "core includes nothing"

[[modules]]
name = "net"
layer = "net"
language = "cpp"
path = "net"
may_depend_on = ["core"]
public = ["net/include"]
decision = "D-layering"
instead = "include net/api.h"

[[modules]]
name = "app"
layer = "app"
language = "cpp"
path = "app"
may_depend_on = ["net", "core"]
decision = "D-layering"
instead = "include net/api.h"

[[modules]]
name = "domain"
layer = "core"
language = "rust"
path = "crates/domain"
decision = "D-layering"
instead = "domain depends on no workspace crate"

[[modules]]
name = "service"
layer = "net"
language = "rust"
path = "crates/service"
may_depend_on = ["domain"]
decision = "D-layering"
instead = "service uses domain only"

[[modules]]
name = "ui"
layer = "app"
language = "rust"
path = "crates/ui"
may_depend_on = ["service"]
decision = "D-layering"
instead = "ui uses service only"

[[dependencies]]
language = "rust"
allow = ["serde"]
decision = "D-crates"
instead = "propose the crate as a D- decision first"
"""

NATIVE_FILES = {
    "core/types.h": "#pragma once\n",
    "net/include/net/api.h": '#pragma once\n#include "../../../core/types.h"\n',
    "net/src/impl.h": '#include "../include/net/api.h"\n',
    "app/main.cpp": '#include "net/api.h"\n#include "../core/types.h"\n',
    "app/app.h": "#pragma once\n",
    "crates/domain/src/lib.rs": "",
    "crates/domain/Cargo.toml": '[package]\nname = "domain"\nversion = "0.1.0"\n\n[dependencies]\nserde = "1"\n',
    "crates/service/src/lib.rs": "",
    "crates/service/Cargo.toml": '[package]\nname = "service"\nversion = "0.1.0"\n\n[dependencies]\ndomain = { path = "../domain" }\n',
    "crates/ui/src/lib.rs": "",
    "crates/ui/Cargo.toml": '[package]\nname = "ui"\nversion = "0.1.0"\n\n[dependencies]\nservice = { path = "../service" }\n',
}


class Repo:
    def __init__(self, test: unittest.TestCase, toml: str, decisions: str, files: dict[str, str]) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.write("docs/architecture.toml", toml)
        self.write("docs/decisions.md", decisions)
        for path, text in files.items():
            self.write(path, text)
        self.git("init", "-q")

    def write(self, path: str, text: str) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def append(self, path: str, text: str) -> None:
        self.write(path, (self.root / path).read_text(encoding="utf-8") + text)

    def replace(self, path: str, old: str, new: str) -> None:
        text = (self.root / path).read_text(encoding="utf-8")
        assert old in text, old
        self.write(path, text.replace(old, new))

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def commit(self) -> None:
        self.git("add", "-A")
        self.git("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "base")

    def arch(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(SCRIPT), *args, "--root", str(self.root)],
                              capture_output=True, text=True, check=False)

    def files(self) -> set[str]:
        """Every file git sees; tools' self-ignoring caches (import-linter's) are not leftovers."""
        listed = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=self.root,
                                capture_output=True, text=True, check=True).stdout
        return set(listed.split())


class ValidateTest(unittest.TestCase):
    def repo(self, toml: str = PY_TOML, decisions: str = PY_DECISIONS) -> Repo:
        return Repo(self, toml, decisions, PY_FILES)

    def test_a_consistent_file_validates(self) -> None:
        result = self.repo().arch("validate")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("validate clean - 9 rules", result.stdout)

    def test_an_upward_edge_is_rejected(self) -> None:
        repo = self.repo(PY_TOML.replace('path = "src/app/domain"\n', 'path = "src/app/domain"\nmay_depend_on = ["ui"]\n'))
        self.assertIn("module domain: depends upward on ui", repo.arch("validate").stdout)

    def test_a_cycle_is_rejected(self) -> None:
        toml = PY_TOML.replace('layers = ["domain", "telemetry", "providers", "service", "ui"]',
                               'layers = ["domain", "telemetry", "providers", "service", "ui"]\n').replace(
            'path = "src/app/telemetry"\n', 'path = "src/app/telemetry"\nmay_depend_on = ["providers"]\n').replace(
            'layer = "telemetry"', 'layer = "providers"')
        self.assertIn("dependency cycle: ", self.repo(toml).arch("validate").stdout)

    def test_a_cross_cutting_concern_imported_directly_is_rejected(self) -> None:
        repo = self.repo(PY_TOML.replace('may_depend_on = ["domain", "providers"]', 'may_depend_on = ["domain", "providers", "telemetry"]'))
        self.assertIn("module service: depends on telemetry directly; cross-cutting concerns enter only through providers",
                      repo.arch("validate").stdout)

    def test_a_banned_rule_without_enough_fixtures_is_rejected(self) -> None:
        repo = self.repo(PY_TOML.replace('valid = ["process.run([\'ls\'])", "my_subprocess.run(cmd)"]', 'valid = []'))
        self.assertIn("needs at least 2 invalid examples (the real mistake) and 2 valid near-misses", repo.arch("validate").stdout)

    def test_a_rule_whose_decision_does_not_list_it_is_rejected(self) -> None:
        repo = self.repo(decisions=PY_DECISIONS.replace("  enforced by: no-raw-subprocess\n", ""))
        self.assertIn("D-no-raw-subprocess does not list no-raw-subprocess on its 'enforced by:' line", repo.arch("validate").stdout)

    def test_a_rule_citing_an_unrecorded_decision_is_rejected(self) -> None:
        repo = self.repo(PY_TOML.replace('decision = "D-deps"', 'decision = "D-missing"'))
        self.assertIn("[dependencies:python] cites D-missing, which docs/decisions.md does not record", repo.arch("validate").stdout)

    def test_a_decision_enforced_by_a_rule_that_does_not_exist_is_rejected(self) -> None:
        repo = self.repo(decisions=PY_DECISIONS.replace("enforced by: providers", "enforced by: providers, no-eval"))
        self.assertIn("D-providers is enforced by no-eval, which docs/architecture.toml does not define", repo.arch("validate").stdout)

    def test_a_decision_may_be_enforced_by_an_existing_test_file(self) -> None:
        repo = self.repo(decisions=PY_DECISIONS.replace("enforced by: providers", "enforced by: providers, test:tests/test_idempotent.py"))
        self.assertIn("test:tests/test_idempotent.py, but that test file does not exist", repo.arch("validate").stdout)
        repo.write("tests/test_idempotent.py", "")
        self.assertEqual(repo.arch("validate").returncode, 0)

    def test_privacy_rules_go_and_rust_enforce_in_code_are_refused(self) -> None:
        repo = Repo(self, NATIVE_TOML.replace('path = "crates/service"\n', 'path = "crates/service"\npublic = ["crates/service/src/lib.rs"]\n'),
                    NATIVE_DECISIONS, NATIVE_FILES)
        self.assertIn("rust enforces privacy in the code itself (pub(crate)); drop 'public'", repo.arch("validate").stdout)


class GenerateTest(unittest.TestCase):
    def test_generate_writes_the_configs_a_vendored_copy_and_a_manifest(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        self.assertEqual(repo.arch("generate").returncode, 0)
        for path in (".arch/importlinter.ini", ".arch/sgconfig.yml", ".arch/ast-grep/rules/no-raw-subprocess.yml",
                     ".arch/ast-grep/tests/no-raw-subprocess-test.yml", ".arch/arch.py", ".arch/manifest.json"):
            self.assertTrue((repo.root / path).is_file(), path)
        ini = (repo.root / ".arch/importlinter.ini").read_text(encoding="utf-8")
        self.assertIn("name = boundary:ui - ui may depend only on service (D-layering). Instead: call app.service.api", ini)
        self.assertIn("forbidden_modules =\n    app.service.**", ini)
        self.assertIn("app.ui.** -> app.service.api", ini)
        rule = (repo.root / ".arch/ast-grep/rules/no-raw-subprocess.yml").read_text(encoding="utf-8")
        self.assertIn('note: "Instead: call the framework\'s process runner"', rule)
        self.assertIn('"src/app/ui/**/*.py"', rule)

    def test_a_hand_edited_config_fails_check_with_how_to_fix_it(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.arch("generate")
        self.assertEqual(repo.arch("check").returncode, 0, repo.arch("check").stdout)
        repo.append(".arch/importlinter.ini", "# loosened by hand\n")
        result = repo.arch("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn(".arch/importlinter.ini differs from what docs/architecture.toml generates. "
                      "Generated files are never edited", result.stdout)

    def test_a_toml_change_without_generate_fails_check(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.arch("generate")
        repo.replace("docs/architecture.toml", 'instead = "call app.service.api, never the domain"', 'instead = "call the api"')
        self.assertIn(".arch/importlinter.ini differs", repo.arch("check").stdout)

    def test_a_removed_rule_takes_its_generated_files_with_it(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS.replace("  enforced by: no-raw-subprocess\n", ""), PY_FILES)
        repo.arch("generate")
        toml = PY_TOML[: PY_TOML.index("[[banned]]")] + PY_TOML[PY_TOML.index("[[dependencies]]"):]
        repo.write("docs/architecture.toml", toml)
        repo.write("docs/decisions.md", PY_DECISIONS.replace("  enforced by: no-raw-subprocess\n", ""))
        repo.arch("generate")
        self.assertFalse((repo.root / ".arch/ast-grep/rules/no-raw-subprocess.yml").exists())
        self.assertEqual(repo.arch("check").returncode, 0)


class RatchetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        self.repo.arch("generate")
        self.repo.commit()

    def test_tightening_needs_no_permission(self) -> None:
        self.repo.replace("docs/architecture.toml", 'may_depend_on = ["domain", "providers"]', 'may_depend_on = ["domain"]')
        self.repo.replace("src/app/service/api.py", "from app.providers import log\n", "")
        self.repo.arch("generate")
        result = self.repo.arch("check", "--base", "HEAD")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_each_loosening_is_reported_against_the_base(self) -> None:
        self.repo.replace("docs/architecture.toml", 'may_depend_on = ["service"]', 'may_depend_on = ["service", "domain"]')
        self.repo.replace("docs/architecture.toml", 'allow = ["requests"]', 'allow = ["requests", "leftpad"]')
        self.repo.replace("docs/architecture.toml", 'public = ["src/app/service/__init__.py", "src/app/service/api.py"]\n', "")
        self.repo.arch("generate")
        out = self.repo.arch("check", "--base", "HEAD").stdout
        self.assertIn("loosening needs a human-reviewed change: module ui may now depend on domain", out)
        self.assertIn("the python dependency allowlist adds leftpad", out)
        self.assertIn("module service no longer restricts imports to its public entries", out)

    def test_loosening_passes_only_when_a_human_allows_it(self) -> None:
        self.repo.replace("docs/architecture.toml", 'may_depend_on = ["service"]', 'may_depend_on = ["service", "domain"]')
        self.repo.arch("generate")
        self.assertEqual(self.repo.arch("check", "--base", "HEAD", "--allow-loosening").returncode, 0)

    def test_the_baseline_only_shrinks(self) -> None:
        self.repo.append("src/app/ui/view.py", "import leftpad\n")
        self.repo.append("docs/architecture.toml", '\n[[baseline]]\nrule = "dependencies:python"\nfrom = "src/app/ui/view.py"\n'
                                                   'to = "leftpad"\nreason = "pre-existing"\n')
        self.repo.arch("generate")
        self.assertEqual(self.repo.arch("check").returncode, 0, self.repo.arch("check").stdout)
        self.assertIn("baseline grew: dependencies:python src/app/ui/view.py -> leftpad", self.repo.arch("check", "--base", "HEAD").stdout)

    def test_a_baseline_entry_whose_violation_is_gone_must_be_deleted(self) -> None:
        self.repo.append("docs/architecture.toml", '\n[[baseline]]\nrule = "dependencies:python"\nfrom = "src/app/ui/view.py"\n'
                                                   'to = "leftpad"\nreason = "pre-existing"\n')
        self.repo.arch("generate")
        self.assertIn("no longer matches a violation; delete it so the ratchet tightens", self.repo.arch("check").stdout)


class RatchetEdgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        self.repo.arch("generate")
        self.repo.commit()

    def test_an_unresolvable_base_ref_is_a_problem_not_a_skip(self) -> None:
        result = self.repo.arch("check", "--base", "no-such-ref")
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read docs/architecture.toml at no-such-ref", result.stdout)

    def test_a_file_absent_at_the_base_has_no_policy_to_loosen(self) -> None:
        self.repo.git("rm", "-q", "--cached", "docs/architecture.toml")
        self.repo.git("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "drop")
        self.repo.git("add", "-A")
        self.repo.git("-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "readd")
        self.assertEqual(self.repo.arch("check", "--base", "HEAD~1").returncode, 0)

    def test_tools_settings_and_layers_count_as_loosening(self) -> None:
        self.repo.append("docs/architecture.toml", '\n[tools]\npython = "true"\n')
        self.repo.replace("docs/architecture.toml", 'root = "src"', 'root = "src"\nextra = 1')
        self.repo.replace("docs/architecture.toml", 'layers = ["domain", "telemetry", ', 'layers = ["telemetry", ')
        out = self.repo.arch("check", "--base", "HEAD").stdout
        self.assertIn("[tools] python changed", out, out)
        self.assertIn("[settings] python changed", out)
        self.assertIn("layer domain was removed", out)

    def test_a_stale_baseline_entry_is_not_hidden_by_another_entry(self) -> None:
        self.repo.append("src/app/ui/view.py", "import leftpad\n")
        self.repo.append("docs/architecture.toml",
                         '\n[[baseline]]\nrule = "dependencies:python"\nfrom = "src/app/ui/view.py"\nto = "leftpad"\nreason = "x"\n'
                         '\n[[baseline]]\nrule = "dependencies:python"\nfrom = "src/app/ui/view.py"\nto = "gone"\nreason = "x"\n')
        self.repo.arch("generate")
        out = self.repo.arch("check").stdout
        self.assertIn("view.py -> gone no longer matches", out)
        self.assertNotIn("view.py -> leftpad no longer matches", out)


class HardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("arch_under_test", SCRIPT)
        cls.arch = importlib.util.module_from_spec(spec)
        sys.modules["arch_under_test"] = cls.arch
        spec.loader.exec_module(cls.arch)

    def test_jvm_common_prefix_is_by_segment(self) -> None:
        shared = self.arch.common_segments([["com", "example", "service"], ["com", "example", "store"]])
        self.assertEqual(".".join(shared), "com.example")

    def test_a_longer_rule_id_does_not_satisfy_the_redteam(self) -> None:
        self.assertFalse(self.arch.names_marker("boundary:api", "boundary:api-v2 broken"))
        self.assertTrue(self.arch.names_marker("boundary:api", "name = boundary:api - x"))

    def test_build_folders_inside_a_module_are_scanned(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, {"pkg/build/gen.py": "x = 1\n", "build/out.py": "y = 1\n"})
        inside = self.arch.source_files(repo.root, "pkg", "python")
        self.assertEqual([p.name for p in inside], ["gen.py"])
        top = self.arch.source_files(repo.root, ".", "python")
        self.assertEqual(sorted(p.name for p in top), ["gen.py"])

    def test_a_python_file_that_does_not_parse_is_reported(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.write("src/app/ui/broken.py", "def (:\n")
        repo.arch("generate")
        self.assertIn("src/app/ui/broken.py: cannot be parsed", repo.arch("check").stdout)

    def test_an_unknown_banned_language_is_reported_not_a_crash(self) -> None:
        repo = Repo(self, PY_TOML.replace('language = "python"\npattern', 'language = "cobol"\npattern'), PY_DECISIONS, PY_FILES)
        for command in ("check", "run"):
            result = repo.arch(command)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("unknown ast-grep language 'cobol'", result.stdout)
            self.assertNotIn("Traceback", result.stderr)

    def test_infer_rejects_depth_zero(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        result = repo.arch("infer", "--language", "python", "--dir", "src/app", "--depth", "0")
        self.assertEqual(result.returncode, 2)
        self.assertNotIn("Traceback", result.stderr)

    def test_an_import_named_like_a_non_python_root_entry_is_not_local(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.write("src/docs/notes.txt", "x\n")
        repo.append("src/app/ui/view.py", "import docs\n")
        repo.arch("generate")
        self.assertIn("src/app/ui/view.py -> docs", repo.arch("check").stdout)


class BuiltinChecksTest(unittest.TestCase):
    def test_an_unlisted_python_package_is_reported_with_its_remediation(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.append("src/app/ui/view.py", "import leftpad\nimport json\n")
        repo.arch("generate")
        out = repo.arch("check").stdout
        self.assertIn("src/app/ui/view.py -> leftpad: [dependencies:python]", out)
        self.assertIn("Instead: propose the package as a D- decision first", out)
        self.assertNotIn("json", out, "the standard library is always allowed")

    def test_native_modules_check_includes_and_crate_edges(self) -> None:
        repo = Repo(self, NATIVE_TOML, NATIVE_DECISIONS, NATIVE_FILES)
        repo.arch("generate")
        self.assertEqual(repo.arch("check").returncode, 0, repo.arch("check").stdout)
        repo.append("app/main.cpp", '#include "../net/src/impl.h"\n')
        repo.append("core/types.h", '#include "../app/app.h"\n')
        repo.append("crates/ui/Cargo.toml", 'domain = { path = "../domain" }\nleftpad = "1"\n')
        out = repo.arch("check").stdout
        self.assertIn("app/main.cpp -> net/src/impl.h: [public:net]", out)
        self.assertIn("core/types.h -> app/app.h: [boundary:core] core may depend on no other module", out)
        self.assertIn("crates/ui/Cargo.toml -> domain: [boundary:ui] ui may depend only on service", out)
        self.assertIn("crates/ui/Cargo.toml -> leftpad: [dependencies:rust]", out)


class RedteamTest(unittest.TestCase):
    def test_every_native_rule_fires_and_nothing_is_left_behind(self) -> None:
        repo = Repo(self, NATIVE_TOML, NATIVE_DECISIONS, NATIVE_FILES)
        repo.arch("generate")
        before = repo.files()
        manifest = (repo.root / "crates/ui/Cargo.toml").read_text(encoding="utf-8")
        result = repo.arch("redteam")
        self.assertEqual(result.returncode, 0, result.stdout)
        for rule in ("boundary:core", "boundary:net", "boundary:domain", "boundary:ui", "public:net", "dependencies:rust"):
            self.assertIn(f"fires  {rule}", result.stdout)
        self.assertIn("skip   boundary:app: app may depend on every other cpp module", result.stdout)
        self.assertEqual(repo.files(), before)
        self.assertEqual((repo.root / "crates/ui/Cargo.toml").read_text(encoding="utf-8"), manifest)

    def test_a_rule_that_cannot_fire_is_reported_green(self) -> None:
        repo = Repo(self, NATIVE_TOML, NATIVE_DECISIONS, NATIVE_FILES)
        # a baseline entry that swallows the planted crate edge turns the rule into a false green
        repo.append("docs/architecture.toml", '\n[[baseline]]\nrule = "dependencies:rust"\nfrom = "crates/domain/Cargo.toml"\n'
                                              'to = "arch-redteam-unlisted"\nreason = "test"\n')
        repo.arch("generate")
        result = repo.arch("redteam")
        self.assertEqual(result.returncode, 1)
        self.assertIn("GREEN  dependencies:rust", result.stdout)
        self.assertIn("stayed green on a planted violation; they enforce nothing until fixed", result.stdout)


class InferTest(unittest.TestCase):
    def test_infer_counts_edges_between_candidate_modules(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        result = repo.arch("infer", "--language", "python", "--dir", "src/app")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("#   service -> domain: 2 import(s)", result.stdout)
        self.assertIn('name = "ui"', result.stdout)
        self.assertIn('may_depend_on = ["service"]', result.stdout)


@unittest.skipUnless(shutil.which("lint-imports") and shutil.which("ast-grep"), "import-linter and ast-grep are not installed")
class PythonToolsTest(unittest.TestCase):
    def test_tools_pass_on_clean_code_and_name_the_broken_rule(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.arch("generate")
        result = repo.arch("run")
        self.assertEqual(result.returncode, 0, result.stdout)
        repo.append("src/app/ui/view.py", "from app.domain import model\n")
        result = repo.arch("run")
        self.assertEqual(result.returncode, 1)
        remediation = result.stdout[result.stdout.index("Remediation:"):]
        remediation = remediation[: remediation.index("ast-grep")]
        self.assertIn("[boundary:ui] ui may depend only on service", remediation)
        self.assertNotIn("[boundary:domain]", remediation, "only the broken contract is named")

    def test_every_rule_fires_on_a_planted_violation(self) -> None:
        repo = Repo(self, PY_TOML, PY_DECISIONS, PY_FILES)
        repo.arch("generate")
        before = repo.files()
        result = repo.arch("redteam")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("every planted violation failed (8 rules)", result.stdout)
        self.assertEqual(repo.files(), before)


GO_TOML = """\
version = 1
languages = ["go"]
layers = ["domain", "service", "cmd"]

[[modules]]
name = "domain"
layer = "domain"
path = "internal/domain"
decision = "D-layering"
instead = "domain imports nothing"

[[modules]]
name = "service"
layer = "service"
path = "internal/service"
may_depend_on = ["domain"]
decision = "D-layering"
instead = "service imports only domain"

[[modules]]
name = "cmd"
layer = "cmd"
path = "cmd"
may_depend_on = ["service"]
decision = "D-layering"
instead = "wire the app through service"
"""


@unittest.skipUnless(shutil.which("go-arch-lint") and shutil.which("go"), "go-arch-lint is not installed")
class GoToolsTest(unittest.TestCase):
    def test_every_rule_fires_on_a_planted_violation(self) -> None:
        files = {
            "go.mod": "module example.com/ag\n\ngo 1.22\n",
            "internal/domain/order.go": "package domain\n\ntype Order struct{}\n",
            "internal/service/service.go": 'package service\n\nimport "example.com/ag/internal/domain"\n\n'
                                           "func Place() domain.Order { return domain.Order{} }\n",
            "cmd/app/main.go": 'package main\n\nimport "example.com/ag/internal/service"\n\nfunc main() { service.Place() }\n',
        }
        decisions = textwrap.dedent("""\
            D-layering: Which way? (2026-11-24, user)
              ✓ down - testable
              ✗ anywhere - coupling
              enforced by: boundary:domain, boundary:service, boundary:cmd
            """)
        repo = Repo(self, GO_TOML, decisions, files)
        repo.arch("generate")
        self.assertEqual(repo.arch("run").returncode, 0, repo.arch("run").stdout)
        result = repo.arch("redteam")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("fires  boundary:domain", result.stdout)


def load_arch_module():
    import importlib.util
    spec = importlib.util.spec_from_file_location("arch_module", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["arch_module"] = module  # dataclasses look their module up while the class is built
    spec.loader.exec_module(module)
    return module


class FallbackTomlTest(unittest.TestCase):
    """On Pythons without tomllib, arch.py reads TOML itself; it must agree with tomllib."""

    EDGES = 'a = "x # not a comment" # a comment\nb = \'C:\\\\raw\'\nc = [\n  "one", # first\n  "two",\n]\n' \
            'd = { path = "../x", features = ["f"] }\n[t.u]\nv = -3\nw = 1.5\nx = true\n[[t.list]]\ny = 1\n[[t.list]]\ny = 2\n' \
            '"quoted key" = "\\u00e9\\n"\n'

    def test_the_built_in_reader_matches_tomllib(self) -> None:
        try:
            import tomllib
        except ModuleNotFoundError:
            self.skipTest("tomllib is not available to compare against")
        arch = load_arch_module()
        sources = [PY_TOML, NATIVE_TOML, GO_TOML, TS_TOML, self.EDGES]
        sources += [text for path, text in NATIVE_FILES.items() if path.endswith("Cargo.toml")]
        for text in sources:
            self.assertEqual(arch._parse_toml(text), tomllib.loads(text))

    def test_the_built_in_reader_rejects_what_it_cannot_read(self) -> None:
        arch = load_arch_module()
        for text in ('a = """multi"""\n', "a = 1979-05-27\n", "a = 1\na = 2\n", 'a = "open\n'):
            with self.assertRaises(arch.TOMLDecodeError):
                arch._parse_toml(text)

    def test_the_whole_cli_runs_on_the_built_in_reader(self) -> None:
        repo = Repo(self, NATIVE_TOML, NATIVE_DECISIONS, NATIVE_FILES)
        env = dict(os.environ, ARCH_TOML_FALLBACK="1")
        for command in ("generate", "check"):
            result = subprocess.run([sys.executable, str(SCRIPT), command, "--root", str(repo.root)],
                                    capture_output=True, text=True, env=env, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


NODE_MODULES = os.environ.get("ARCH_TEST_NODE_MODULES", "")
TS_TOML = """\
version = 1
languages = ["typescript"]
layers = ["domain", "service", "ui"]

[settings.typescript]
tsconfig = "tsconfig.json"

[tools]
typescript = "node_modules/.bin/depcruise --config .arch/dependency-cruiser.cjs --ignore-known .arch/dependency-cruiser-known.json {paths}"

[[modules]]
name = "domain"
layer = "domain"
path = "src/domain"
decision = "D-layering"
instead = "domain imports nothing"

[[modules]]
name = "service"
layer = "service"
path = "src/service"
may_depend_on = ["domain"]
public = ["src/service/index.ts"]
decision = "D-layering"
instead = "import from src/service"

[[modules]]
name = "ui"
layer = "ui"
path = "src/ui"
may_depend_on = ["service"]
decision = "D-layering"
instead = "call the service through src/service"

[[dependencies]]
language = "typescript"
allow = ["react"]
decision = "D-deps"
instead = "propose the package as a D- decision first"
"""


@unittest.skipUnless(NODE_MODULES and Path(NODE_MODULES, ".bin", "depcruise").exists(),
                     "set ARCH_TEST_NODE_MODULES to a node_modules with dependency-cruiser and typescript")
class TypeScriptToolsTest(unittest.TestCase):
    def test_every_rule_fires_and_the_baseline_admits_only_its_entry(self) -> None:
        files = {
            "src/domain/order.ts": "export class Order {}\n",
            "src/service/index.ts": 'import { Order } from "../domain/order";\nexport const place = () => new Order();\n',
            "src/service/internal.ts": "export const secret = 1;\n",
            "src/ui/view.ts": 'import { place } from "../service";\nexport const view = () => place();\n',
            "tsconfig.json": '{"compilerOptions": {"module": "esnext", "moduleResolution": "bundler", "strict": true}}\n',
            "package.json": '{"name": "fixture", "version": "1.0.0"}\n',
            ".gitignore": "node_modules\n",
        }
        decisions = PY_DECISIONS.replace(
            "boundary:domain, boundary:telemetry, boundary:providers, boundary:service, boundary:ui, public:service",
            "boundary:domain, boundary:service, boundary:ui, public:service").replace(
            "enforced by: dependencies:python", "enforced by: dependencies:typescript")
        decisions = decisions.replace("  enforced by: providers\n", "").replace("  enforced by: no-raw-subprocess\n", "")
        repo = Repo(self, TS_TOML, decisions, files)
        (repo.root / "node_modules").symlink_to(NODE_MODULES)
        repo.arch("generate")
        self.assertEqual(repo.arch("run").returncode, 0, repo.arch("run").stdout)
        result = repo.arch("redteam")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("every planted violation failed (5 rules)", result.stdout)
        self.assertFalse(Path(NODE_MODULES, "arch-redteam-unlisted").exists())
        repo.write("src/ui/legacy.ts", 'import { Order } from "../domain/order";\nexport const legacy = new Order();\n')
        repo.append("docs/architecture.toml", '\n[[baseline]]\nrule = "boundary:ui"\nfrom = "src/ui/legacy.ts"\n'
                                              'to = "src/domain/order.ts"\nreason = "pre-existing"\n')
        repo.arch("generate")
        self.assertEqual(repo.arch("run").returncode, 0, repo.arch("run").stdout)
        repo.write("src/ui/fresh.ts", 'import { Order } from "../domain/order";\nexport const fresh = new Order();\n')
        result = repo.arch("run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("error boundary:ui: src/ui/fresh.ts", result.stdout)
        self.assertNotIn("legacy.ts", result.stdout)


if __name__ == "__main__":
    unittest.main()
