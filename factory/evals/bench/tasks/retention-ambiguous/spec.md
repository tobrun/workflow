# Dedup retention

2026-09-15

## Research

D-dedup-store: Where are processed webhook ids remembered?
  ✓ database table - survives restarts and the database is already there
  ✗ in-memory set - lost on every restart

D-retention: How long are processed ids remembered?
  ✓ forever - the spec does not bound it; reopen if memory grows
  ✗ a time window - no window was specified

## Scope

Inputs: webhook deliveries. Outputs: each delivery id is processed once.

### Validation

- `readme`: `test -f README.md`

## Change plan

1. Deduplicate deliveries
   a. webhook.py - skip ids already processed - decisions: D-dedup-store (✓ database table)
   tests: [unit] [repro] repeated id -> ignored; [e2e] duplicate delivery -> processed once
