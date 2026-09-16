"""Factory runner: a durable local workflow over the factory pipeline skills.

The package is stdlib-only. `factory/bin/factory` puts the `factory/` directory
on PYTHONPATH and runs `python3 -m runner`.
"""

from pathlib import Path

FACTORY_ROOT = Path(__file__).resolve().parent.parent
