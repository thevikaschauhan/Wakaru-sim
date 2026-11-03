"""Offline contract conformance and manifest verification."""

import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from cart_recovery.domain.knowledge import SCHEMA, read_knowledge  # noqa: E402
import jsonschema  # noqa: E402


def main():
    directory = Path(__file__).resolve().parent
    manifest = json.loads((directory / "manifest.json").read_text())
    for name, expected in manifest["sha256"].items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("knowledge contract manifest drift: " + name)
    jsonschema.Draft202012Validator.check_schema(SCHEMA)
    fixtures = json.loads((directory / "fixtures.json").read_text())
    for fixture in fixtures:
        bindings = fixture["bindings"]
        bindings["at"] = datetime.fromisoformat(bindings["at"].replace("Z", "+00:00"))
        read_knowledge(json.dumps(fixture["payload"]), **bindings)
    print(json.dumps({"version": "knowledge_batch_v1", "fixtures_passed": len(fixtures),
                      "manifest_files_verified": len(manifest["sha256"]),
                      "runtime_activation": False}))


if __name__ == "__main__":
    main()
