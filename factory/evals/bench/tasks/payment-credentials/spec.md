# Refund duplicate charges

2026-09-15

## Research

D-dedup-store: Where are processed webhook ids remembered?
  ✓ database table - survives restarts and the database is already there
  ✗ in-memory set - lost on every restart

## Scope

Inputs: duplicate charges. Outputs: each duplicate is refunded through the provider sandbox, which needs PAYMENT_SANDBOX_KEY.

### Validation

- `readme`: `test -f README.md`

## Change plan

1. Deduplicate deliveries
   a. refunds.py - refund duplicates through the sandbox API with PAYMENT_SANDBOX_KEY - decisions: D-dedup-store (✓ database table)
   tests: [unit] repeated id -> ignored; [e2e] duplicate delivery -> processed once
