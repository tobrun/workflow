"""Cost estimates only from an explicit, versioned price table; never from assumptions.

`~/.factory/pricing.json` (optional):

    {"schema": "factory.pricing/1", "version": "2026-09-01", "currency": "USD", "source": "https://...",
     "models": {"openai.gpt-5.6-luna": {"input_per_mtok": 1.25, "cached_input_per_mtok": 0.125,
                                        "cache_write_input_per_mtok": 1.25, "output_per_mtok": 10.0}}}

Cached and cache-write input are priced as parts of input, reasoning output as part of
output, matching how the host reports them. A model without a price, or usage the host did
not report, yields an unknown estimate rather than a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA = "factory.pricing/1"
FIELDS = ("input_per_mtok", "cached_input_per_mtok", "cache_write_input_per_mtok", "output_per_mtok")


def load(home: Path) -> dict | None:
    path = Path(home) / "pricing.json"
    if not path.is_file():
        return None
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if table.get("schema") != SCHEMA or not isinstance(table.get("models"), dict):
        return None
    return table


def estimate(table: dict | None, model: str, usage: dict | None) -> dict:
    if table is None:
        return {"amount": None, "why": "no pricing table (~/.factory/pricing.json)"}
    prices = table["models"].get(model)
    if not prices or not all(isinstance(prices.get(field), (int, float)) for field in FIELDS):
        return {"amount": None, "why": f"no complete price for {model} in pricing {table.get('version')}"}
    if not usage or not usage.get("reported"):
        return {"amount": None, "why": "the host reported no usage"}
    cache_write = min(usage["cache_write_input"], usage["uncached_input"])
    amount = (usage["cached_input"] * prices["cached_input_per_mtok"]
              + cache_write * prices["cache_write_input_per_mtok"]
              + (usage["uncached_input"] - cache_write) * prices["input_per_mtok"]
              + usage["output"] * prices["output_per_mtok"]) / 1_000_000
    return {"amount": round(amount, 4), "currency": table.get("currency", "USD"), "pricing_version": table.get("version")}


def short(count: int | None) -> str:
    if count is None:
        return "unknown"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def describe(usage: dict) -> str:
    """One line that never presents cached input as if it were uncached."""
    if not usage or usage.get("total") is None or (usage.get("reported") is False):
        return "usage unknown (the host reported none)"
    return (f"{short(usage['total'])} tokens: {short(usage['cached_input'])} cached input, "
            f"{short(usage['uncached_input'])} uncached input ({short(usage['cache_write_input'])} cache writes), "
            f"{short(usage['output'])} output ({short(usage['reasoning_output'])} reasoning)")
