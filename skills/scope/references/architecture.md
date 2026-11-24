# Architecture Enforcement

`docs/architecture.toml` in the consuming project: one human-owned file stating which modules exist, which way their dependencies point, what each exposes, which APIs are banned where, and which external dependencies are allowed.
`arch.py` ([../scripts/arch.py](../scripts/arch.py), vendored into the project at `.arch/arch.py`) generates every tool config from it, so design intent and enforcement cannot drift apart.
Each rule cites the `D-` decision in `docs/decisions.md` that made it, and that decision lists the rule back on an `enforced by:` line.

## The ladder

Every architectural decision lands on the strongest level that can hold it:

1. **Structure** makes the violation impossible: a separate package, crate, Gradle module, or Bazel target; Go `internal/`; Rust `pub(crate)`; an API that offers no wrong way to call it.
2. **Static analysis** fails CI on it: a generated boundary rule, an ast-grep rule, a type.
3. **Runtime or formal verification** for what no static check sees: a property-based test for idempotence, ordering, or a state machine, cited as `test:{path}` on the decision's `enforced by:` line.
4. **Prose** in AGENTS.md or review, for taste only. Agents skip it and it rots, so a correction that recurs moves up the ladder: first a rule, then a redesign that needs no rule.

| Kind of decision | Mechanism |
| --- | --- |
| Module A may or may not depend on B | `[[modules]]` `may_depend_on`, generated into the boundary tool |
| Import X only through its public API | `public` entries on the module, or structure (Go, Rust) |
| Cross-cutting concerns enter in one place | `[providers]`: the concern modules may be imported only by the providers module |
| Banned API, required wrapper | `[[banned]]` ast-grep rule, allowed only in the framework modules that wrap it |
| Third-party dependency policy | `[[dependencies]]` allowlist |
| Dataflow (PII never reaches the logger) | Semgrep taint mode or CodeQL, outside this file, cited as `test:{config path}` |
| Runtime invariant | property test, cited as `test:{path}` |
| Pure taste | review only; never encode it |

## The file

```toml
version = 1
languages = ["python"]                 # python, typescript, go, jvm, rust, cpp
layers = ["domain", "service", "ui"]   # lowest first; edges point down or sideways, never up

[settings.python]
root = "src"                           # the import root

[[modules]]
name = "ui"
layer = "ui"
path = "src/app/ui"
may_depend_on = ["service"]            # everything else is forbidden
public = ["src/app/ui/__init__.py"]    # optional: outsiders import only these
decision = "D-layering"
instead = "call the service through app.service.api"

[providers]
module = "providers"
concerns = ["telemetry", "auth"]
decision = "D-providers"
instead = "use app.providers"

[[banned]]
id = "no-raw-subprocess"
language = "python"                    # an ast-grep language
pattern = "subprocess.$METHOD($$$ARGS)"
message = "raw process spawning needs judgment about shells and timeouts"
instead = "call app.framework.process.run(cmd)"
allowed_in = ["framework"]
decision = "D-no-raw-subprocess"
invalid = ["subprocess.run(['ls'])", "subprocess.Popen(cmd, shell=True)"]   # the real mistake
valid = ["process.run(['ls'])", "my_subprocess.run(cmd)"]                   # near-misses that must pass

[[dependencies]]
language = "python"
allow = ["requests", "pydantic"]
decision = "D-dependencies"
instead = "propose the package as a D- decision first"

[[baseline]]                           # existing violations; the list only shrinks
rule = "boundary:ui"
from = "src/app/ui/legacy.py"
to = "src/app/repo"
reason = "pre-existing, burn down"
```

Rule ids: `boundary:{module}`, `public:{module}`, `providers`, each banned `id`, and `dependencies:{language}`.
The decision behind them says `enforced by: boundary:ui, public:service`.
Other settings: `[settings.typescript] tsconfig`, `[settings.go] module` (default: go.mod), `[settings.jvm] test_file`, `test_package`, `base_package`, `[settings.cpp] include_roots`, and `[tools]` to override a tool command.

## What enforces each ecosystem

| Ecosystem | Boundaries and public API | Dependency allowlist |
| --- | --- | --- |
| Python | import-linter forbidden contracts (`.arch/importlinter.ini`) | built-in check |
| TS/JS | dependency-cruiser (`.arch/dependency-cruiser.cjs`; needs `typescript` installed to read `.ts`) | dependency-cruiser |
| Go | go-arch-lint (`.arch/go-arch-lint.yml`); privacy is Go `internal/` | go-arch-lint vendors |
| JVM, Kotlin | ArchUnit test class at `test_file`; baselined rules are frozen with `FreezingArchRule` | the build's dependency verification |
| Rust | crates are modules: built-in check of each crate's `Cargo.toml`; privacy is `pub(crate)` | built-in check |
| C++ | built-in check of quoted `#include`s; with Bazel, prefer `visibility` | the build's lockfile |
| All | banned APIs: ast-grep (`.arch/sgconfig.yml`, rules and fixtures under `.arch/ast-grep/`) | |

One tool per job and language: a second boundary tool duplicates configs and contradicts messages.
Prefer structure where the project can afford it: a build graph makes the violation unbuildable, a linter only catches it after it is written.

## Rules that tell the truth

- **Messages are instructions.** Every message names the rule, the decision, and the sanctioned alternative: `[boundary:ui] ui may depend only on service (D-layering). Instead: call the service through app.service.api`.
- **Fixtures before the rule.** A banned rule carries at least two invalid examples from the real mistake and two valid near-misses; `arch.py run` runs them as ast-grep tests.
- **Red team before trusting.** `arch.py redteam` plants one violation per rule at a real path, runs the tools, and fails on any rule that stays green. A tool that silently parses nothing (dependency-cruiser without `typescript`, ast-grep with no rules loaded) shows up here, not in a green CI.
- **Narrow over broad.** An over-broad rule pushes agents into contortions; an aspirational rule the code does not follow erodes trust in all of them. Encode what the code does today, baseline the rest.

## Who may change what

- `docs/architecture.toml` is the only legitimate change path; generated files are never edited, and `arch.py check` fails on any drift.
- Agents may add and tighten rules. Loosening - a removed rule, a new edge, a wider `allowed_in`, a longer allowlist, fewer public restrictions, a grown baseline - is a human call: `arch.py check --base {default branch}` fails on it unless a human-reviewed change passes `--allow-loosening`.
- Never suppress a violation inline. A violation the change seems to need is either a design problem or a decision to reopen with the user.
- CI is authoritative: it runs `arch.py run --base origin/{default}` for every change, whoever wrote it.
