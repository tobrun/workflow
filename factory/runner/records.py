"""Runner adapter for the shared record validators.

The validators live in `factory/scripts/factory_records.py` so the generated Codex
plugin ships them to skill scripts; the runner loads the same file by path.
"""

from __future__ import annotations

import importlib.util
import sys

from runner import FACTORY_ROOT

PATH = FACTORY_ROOT / "scripts" / "factory_records.py"


def _load():
    existing = sys.modules.get("factory_records")
    if existing is not None and getattr(existing, "__file__", None) == str(PATH):
        return existing
    spec = importlib.util.spec_from_file_location("factory_records", PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["factory_records"] = module
    spec.loader.exec_module(module)
    return module


_module = _load()
for _name in _module.__all__:
    globals()[_name] = getattr(_module, _name)
__all__ = list(_module.__all__)
