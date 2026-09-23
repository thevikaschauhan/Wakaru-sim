"""Synthetic-only cross-service receipt. No customer data, tools, or outreach."""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from .recovery import RecoveryWorkflow
from .test_recovery import cart, Judge
from .typesafe import configured_store
from ..buyer_state import direct_insight

p = argparse.ArgumentParser()
p.add_argument("--output", required=True)
p.add_argument("--context")
p.add_argument("--live", action="store_true")
p.add_argument("--journal", required=True)
args = p.parse_args()
c = cart()
if args.context:
    c.intelligence_context = json.loads(Path(args.context).read_text())
    c.episode_id = c.intelligence_context["episode_id"]
merchant = c.intelligence_context["merchant_id"]
judge = configured_store(args.journal) if args.live else Judge()
judge.path = args.journal
result = RecoveryWorkflow(judge).run(c, direct_insight(c), merchant, "assist")
Path(args.output).write_text(json.dumps(asdict(result), indent=2))
replay = RecoveryWorkflow(judge).run(c, direct_insight(c), merchant, "assist")
assert replay.recovery_plan == result.recovery_plan
if args.live:
    judge.client.close()
print(
    json.dumps(
        {
            "status": result.buyer_intelligence["status"],
            "action": result.recovery_plan["action"],
            "plan_revision": result.recovery_plan["revision"],
            "replay_stable": True,
            "allow_incentive": result.recovery_plan["allow_incentive"],
        }
    )
)
