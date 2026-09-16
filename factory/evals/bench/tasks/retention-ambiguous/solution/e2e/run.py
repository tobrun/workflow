import json
import os
import sys

sys.path.insert(0, os.getcwd())
from webhook import Webhooks

out = os.environ["FACTORY_E2E_OUT"]
hooks = Webhooks()
before = {"processed": len(hooks.processed)}
first, second = hooks.deliver("d1", {"n": 1}), hooks.deliver("d1", {"n": 1})
after = {"processed": len(hooks.processed)}
passed = after["processed"] == 1 and second == "ignored"
record = {
    "schema": "factory.e2e/1", "plan": "webhook", "kind": "non-frontend", "revision": os.environ["FACTORY_REVISION"],
    "scenarios": [{
        "id": "duplicate-delivery", "title": "a duplicate delivery is processed once",
        "given": "a delivery d1", "when": "d1 arrives twice", "then": "it is processed once",
        "status": "pass" if passed else "fail",
        "states": [{"step": "deliver d1 twice", "entity": "processed deliveries", "before": before, "after": after}],
        "assertions": [{"name": "processed once", "passed": passed, "detail": f"responses {first}, {second}"}],
        "output": f"{first} then {second}",
    }],
}
with open(os.path.join(out, "e2e.json"), "w", encoding="utf-8") as handle:
    json.dump(record, handle)
sys.exit(0 if passed else 1)
