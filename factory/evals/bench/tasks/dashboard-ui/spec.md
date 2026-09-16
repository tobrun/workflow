# Delivery dashboard

2026-09-15

## Research

D-dedup-store: Where are processed webhook ids remembered?
  ✓ database table - survives restarts and the database is already there
  ✗ in-memory set - lost on every restart

## Scope

Inputs: webhook deliveries. Outputs: each delivery id is processed once.

### Validation

- `readme`: `test -f README.md`

## Change plan

1. Deduplicate deliveries
   a. webhook.py - skip ids already processed - decisions: D-dedup-store (✓ database table)
   tests: [unit] render lists processed ids; [e2e] dashboard page -> shows processed ids and the ignored badge
