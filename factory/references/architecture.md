# Architecture Overview

`docs/architecture.md` in the consuming project: one repo-tracked file describing the system at high level - what it is for, which components make it up, how they talk, and where its boundaries with the outside world lie.
It is the page a new engineer reads on day one and an agent reads before touching anything, and it exists because every run otherwise rebuilds that picture from the file tree, gets it subtly wrong, and never writes it down.

It is not a registry and not a ledger: the decisions, contracts, and dependency rules live in their own files with their own notation and consumers.
This file is plain prose and tables at the altitude of the whole system, and nothing in it is machine-enforced beyond staying current with the code it describes.

## Notation

```
# Architecture

Purpose: one paragraph - what the system does, for whom, and the shape of a request through it.
Captured: 2026-09-15 (full, ship) - Updated: 2026-09-18 (commit 4f2a1c9)

## Components

| Component | Responsibility | Lives at | Talks to |
| --------- | -------------- | -------- | -------- |
| api | HTTP surface, auth, request validation | `src/api/` | app |
| app | use cases, orchestration, transactions | `src/app/` | domain, infra |
| domain | pure business rules and entities | `src/domain/` | nothing |
| infra | Postgres, payment-svc client, mail | `src/infra/` | domain (types only) |
| worker | queue consumers for async jobs | `src/worker/` | app |

## Flows

### Checkout
1. `POST /checkout` (api) validates the cart and calls `placeOrder` (app).
2. app reserves stock in domain, writes the order via infra, enqueues `charge-order`.
3. worker charges through payment-svc and emails the receipt.

## Boundaries

| Boundary | Kind | Owned by | Notes |
| -------- | ---- | -------- | ----- |
| Postgres | store | infra | single database, migrations in `db/migrations/` |
| payment-svc | external HTTP | infra | webhooks arrive at `POST /webhooks/payment` (api) |
| Redis queue | queue | worker | at-least-once delivery, consumers are idempotent |

## Cross-cutting

Auth: JWT checked in api middleware, `requireUser`; config: `src/config.ts` from env only; observability: structured logs, OpenTelemetry traces from api and worker.

## Entry points

`src/api/main.ts` (HTTP server), `src/worker/main.ts` (queue consumer), `scripts/migrate.ts`.
```

Five sections in this order - Components, Flows, Boundaries, Cross-cutting, Entry points - under a Purpose paragraph and a dated `Captured` / `Updated` line.
Every component names the path it lives at, in backticks - that is what lets the checker tell a stale overview from a current one.
Flows are the three to six journeys that explain why the components exist, as numbered steps naming the component at each hop; not every endpoint, not every function.
Boundaries are everything the system does not own: stores, queues, external services, files, clocks worth naming.

## What belongs here

The level a new engineer needs on day one and a reviewer needs before reading a diff: components and their responsibilities, the main flows, the boundaries, the cross-cutting mechanisms, the entry points.
Not here: function-level detail, per-endpoint lists, configuration values, decisions and their alternatives, boundary guarantees, allowed dependency edges - those have their own files.
Keep it under roughly 150 lines; an overview that needs more is describing more than one system, or describing it too closely.

## The checker

`architecture-check.py` (in this toolkit's shared `scripts/`) only checks that the overview is still true of the code, nothing more:

```bash
python3 {skill-root}/../../scripts/architecture-check.py docs/architecture.md [--touched file ...]
```

- Exit 2: the file is missing - the caller runs an initial capture, never a fix agent.
- Exit 1: violations, one per line - a required section missing, a backticked path that no longer exists, or a `--touched` file no component's path covers (`uncharted`).
- Exit 0: the overview is current for what was checked.

## Initial capture

When a skill needs the overview and the file is absent, capture the whole system in one pass before continuing - a partial overview that only covers the current change misleads the next reader more than none.
Dispatch read-only explorers in a single message, one per top-level source area (the directories under the source root, or the packages of a monorepo), each reporting for its area: components with paths and responsibilities, what they call and what calls them, external boundaries they touch, entry points, and the cross-cutting mechanisms they participate in.
Merge their results into the notation above, resolving each flow end to end across areas, then loop the checker until it exits 0.
Write the `Captured` line with the date and the skill, tell the user the overview is new and where it is, and commit it as its own `docs(architecture): capture the system overview` commit.
Never fabricate a component or flow to make the overview look complete; an area the explorers could not characterize is listed with `unverified` in its responsibility cell, so the next reader knows to look.

## Continuous updates

The overview changes when the system changes, in the same commit or batch:

- **build** updates it as part of the change set that adds, removes, renames, or moves a component, changes a flow, or adds a boundary, and runs the checker before that change set's commit.
- **commit** runs the checker over every batch (its sync-checks step 4d): a touched file that is uncharted, a stale path, or a flow or boundary the diff visibly changed is a targeted edit committed as `docs(architecture): ...`, never a rewrite.
- **scope** starts its prior-art explorers from the overview, and names the components and flows a change will add or reshape in the spec's scope section so build knows what to update.
- **ship**'s architecture lens reads it for orientation - where things live and how they talk - and treats a diff that reshapes a component or flow without updating the overview as a finding.

Edits are minimal and factual: change the row, step, or cell that is now wrong, refresh the `Updated` line, leave the rest alone.
A section rewritten in a run that touched one component is a sign the run drifted into documentation work it was not asked for.
