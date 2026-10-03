#!/usr/bin/env python3
"""A stub phase: write a conforming done result to the path in argv[1]."""

import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(
    json.dumps({"schema": "factory.result/1", "phase": path.stem, "status": "done", "reason": ""}),
    encoding="utf-8",
)
