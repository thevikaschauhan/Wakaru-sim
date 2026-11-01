"""Read-only AF10 accounting. Unique attempts, including crashes, are counted once.
No provider call, raw prompts, model replies or consumer send authority.
"""
from datetime import datetime, timezone
from decimal import Decimal
import os
from .journal import Conflict


def measurement(journal, merchant, journey):
    environments = [journal.environment]
    shadow = os.getenv("AF10_SHADOW_ENVIRONMENT", "")
    if shadow:
        if not shadow.startswith("shadow-") or shadow == journal.environment:
            raise Conflict("invalid_shadow_environment")
        environments.append(shadow)
    with journal.transaction() as db:
        rows = db.execute("""SELECT a.environment,a.id::text,a.reserved,a.charged,a.result,a.finished_at,
          extract(epoch from (a.finished_at-a.started_at))*1000
          FROM af6_attempts a JOIN af6_allocations x ON x.attempt=a.id AND x.scope='journey'
          WHERE a.environment=ANY(%s) AND a.merchant=%s AND x.identity=%s AND x.environment=a.environment ORDER BY a.started_at,a.id LIMIT 10001""",
          (environments,merchant,merchant+":"+journey)).fetchall()
    if len(rows)>10000:
        raise Conflict("measurement_overflow")
    items=[]
    for environment,ident,reserved,charged,result,finished,elapsed in rows:
        cost=(result or {}).get("cost")
        known=isinstance(cost,(int,float,Decimal)) and not isinstance(cost,bool) and Decimal(str(cost)).is_finite() and cost>=0
        # Current adaptive serving workers use this environment. Research must use
        # a separate shadow environment so it cannot enter serving cost comparisons.
        traffic="shadow" if environment.startswith("shadow-") else "serving"
        items.append(dict(id=ident,revision=0,kind="semantic",traffic=traffic,calls=1,
            known_micros=int(charged) if known else 0,
            reserved_micros=0 if known else int(charged),unknown_attempts=0 if known else 1,
            repair=0,elapsed_ms=int(elapsed) if elapsed is not None else None))
    return dict(source="wakaru",journey_id=journey,measured_at=datetime.now(timezone.utc).isoformat(),items=items,checks=[])
