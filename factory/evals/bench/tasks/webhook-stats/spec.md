# Delivery statistics

2026-09-15

## Research

D-stats-source: Where do delivery counts come from?
  ✓ the processed list - it already records every processed id
  ✗ a separate counter - a second source of truth

## Scope

Inputs: webhook deliveries. Outputs: a stats() summary of processed and ignored deliveries.

### Validation

- `readme`: `test -f README.md`

## Change plan

1. Delivery statistics
   a. webhook.py - count processed and ignored deliveries - decisions: D-stats-source (✓ the processed list)
   tests: [unit] two deliveries and one repeat -> stats processed 2 ignored 1
