"""Cross-repository stdin/stdout fixture. No networking, storage or model calls."""
import json
import sys
from datetime import datetime, timezone
from .intelligence.bounded_context import interpret


def main():
    raw = sys.stdin.buffer.read(200001)
    value = json.loads(raw)
    c = value["context"]
    customer = c["customer_context"]
    result = interpret(raw, merchant=c["merchant_id"], store=customer["store_id"], subject=customer["subject_id"], episode=c["episode_id"], now=datetime.now(timezone.utc))
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
