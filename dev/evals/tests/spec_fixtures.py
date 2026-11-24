"""Spec fragments the scope script tests share."""

from __future__ import annotations

KINDS = (
    "unit", "functional", "component", "contract", "property", "fuzz", "formal", "static-analysis", "lint",
    "coverage", "mutation", "regression", "acceptance", "smoke", "benchmark", "performance", "ui-ux",
    "usability", "threat-model", "penetration",
)


def verification(*applies: str, how: dict[str, str] | None = None) -> str:
    """A complete ### Verification block: the named kinds yes, every other kind no with a reason."""
    how = how or {}
    lines = [f"- {kind}: yes - {how.get(kind, 'covered by this change')}" if kind in applies
             else f"- {kind}: no - not touched by this change" for kind in KINDS]
    return "### Verification\n\n" + "\n".join(lines) + "\n\n"
