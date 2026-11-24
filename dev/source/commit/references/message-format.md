# Commit Message Format

## Type

What kind of change:

| Type | When to use |
|---|---|
| `feat` | New capability - screen, flow, integration, endpoint |
| `enhance` | Extend existing feature with new behavior or options |
| `adjust` | Small behavioral tweak - copy, defaults, UX polish, config values |
| `fix` | Bug fix - something was broken, now it works |
| `refactor` | Restructure without behavior change |
| `test` | Add or fix tests |
| `perf` | Performance improvement |
| `chore` | Build, deps, config, tooling |
| `docs` | Documentation |
| `style` | Formatting only, no logic change |

## Scope

The product area or module, derived from which part of the codebase the files live in.
Use short, lowercase names: `auth`, `nav`, `onboarding`, `payments`, `cart`, `profile`, `settings`, etc.
Scope slugs are also the decision ledger's area vocabulary (see [../../../references/decision-ledger.md](../../../references/decision-ledger.md)) - keep them stable.

## Subject line

- Format: `type(scope): description`
- Imperative mood, under 72 characters.
- The subject captures the **what**.
- If the user provided a message argument and there's only one commit, use it as the description portion.

## Body

The body MUST include both **what** changed and **why** it was done.
Commit text is free - when doing archaeology, more detail is always better than less.
All sections can and should be multi-line when the change warrants it. Wrap at 72 characters.

- Lead with `What:` - describe the concrete changes thoroughly. List specific items added, removed, renamed, or restructured. Name the functions, fields, files, and patterns involved. The subject line is a label; the What section is where someone learns what actually happened without reading the diff.
- Blank line, then `Why:` - explain the motivation in depth. What problem existed before? What was wrong or missing? What goal does this serve? What constraint or trigger drove this change?
- Optionally `Considered:` - alternatives evaluated and rejected, and why. The diff shows what you chose; this section captures what you didn't and why not. Include only when a meaningful alternative existed - don't force it for straightforward changes.
- Optionally `Constraint:` - external forces that shaped the decision. API limits, legal requirements, platform bugs, vendor quirks, backwards compatibility promises, deadline pressure. Without this, someone refactoring later may "improve" the code in a way that violates an invisible rule.
- Optionally `Directive:` - forward-looking warnings for future modifiers. "If you change X, you must also update Y." "This pattern is intentional because of Z - don't simplify it." Include only when there's a non-obvious coupling or intentional pattern that could be mistakenly "fixed".
- For `fix` commits, `Symptoms:` - the observable failure before the fix: error messages, stack traces, user-visible behavior, log output. This makes the commit findable via `git log --grep` when someone hits the same error later.

## Trailers

Place trailers at the end of the message body, separated by a blank line.
One trailer per line, `Key: value` format. Only include trailers that have values.
Order: Severity -> Risk -> Mobile -> Platform -> Affects -> Breaking -> Refs.

**Severity** (required for `fix`, optional otherwise):

- `critical` - data loss, security breach, crash, complete feature broken
- `high` - major feature degraded, significant UX broken
- `moderate` - minor feature issue, edge case, degraded experience
- `low` - cosmetic, typo, minor inconvenience

**Risk** (include when the change touches a sensitive domain):

- `security` - auth, tokens, encryption, permissions, PII
- `data` - database, migrations, storage, sync, data integrity
- `money` - payments, billing, subscriptions, pricing
- `ux` - user-facing behavior, accessibility, navigation
- `infra` - CI/CD, deploys, monitoring, config

`Severity:` and `Risk:` also feed the decision ledger's per-area risk lines (see [../../../references/decision-ledger.md](../../../references/decision-ledger.md) Area headers).

**Affects** - comma-separated screens, flows, or areas impacted beyond the files changed.
Derive from imports, navigation references, and screen-level file paths. Omit if the impact is limited to the files themselves.

**Refs** - issue/ticket references. Derive from the branch name if it contains an issue number (e.g. `fix/142-auth-race` -> `#142`). Omit if none found.

**Breaking** - include `Breaking: yes` ONLY when the change breaks an existing API, interface, or contract. Omit entirely otherwise.

**Platform** (mobile projects only - include ONLY when the change is platform-specific):

- `ios` - changes in `.swift`, `.m`, `.h`, `ios/`, `Podfile`, Xcode configs
- `android` - changes in `.kt`, `.java`, `android/`, `build.gradle`
- Omit entirely for cross-platform code (`.ts`, `.tsx`, shared modules).

**Mobile** (mobile projects only - include when the change involves a known mobile pain point, derived from the code patterns in the diff):

- `background` - app suspend/resume, background tasks, state loss, `AppState` listeners
- `startup` - cold/warm launch, splash screen, initial data load
- `offline` - cache, sync, conflict resolution, `NetInfo`, no-network handling
- `deeplink` - URL schemes, universal links, `Linking` API, deferred deep links
- `push` - notifications, badges, background delivery, notification handlers
- `navigation` - stack state, tab memory, back behavior, gesture handlers, navigation refs
- `keyboard` - keyboard avoidance, dismissal, input focus, `KeyboardAvoidingView`
- `permissions` - camera, location, notifications, app tracking, permission requests
- `storage` - AsyncStorage, keychain, SecureStore, MMKV, migrations, data corruption
- `lifecycle` - foreground/background transitions, memory pressure, `useAppState`

Multiple mobile values are allowed, comma-separated.
Omit the trailer entirely for non-mobile projects or when no pain point applies.
