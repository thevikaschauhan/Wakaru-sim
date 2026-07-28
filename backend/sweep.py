"""One-shot orphan-graph sweep entrypoint (issue #72).

Runs a SINGLE ``zep_graph_sweeper.sweep_orphan_graphs`` pass and exits. Deployed
as a dedicated Railway **cron** service (``railway.sweep.toml``: start command
``python sweep.py``, hourly ``cronSchedule``) — Railway's cron IS the scheduler,
so this replaces the previous self-perpetuating RQ scheduler chain
(marker/lock/generation-ownership/killed-horse/reconciler, all removed). The
recurring liveness story is now boring: Railway either fires the cron or it does
not (visible in the Railway run history), and the Sentry Cron Monitor check-in
below alerts on a missed run from outside this process. There is no in-worker
chain that can silently die.

Config is the same ``ZEP_SWEEP_*`` env (read live by ``zep_graph_sweeper``);
ships **DRY-RUN** (``ZEP_SWEEP_DRY_RUN`` unset ⇒ dry) until the operator enables
deletion (OP1). The Railway cron cadence MUST match ``ZEP_SWEEP_INTERVAL_MINUTES``
(default 60) so the Sentry monitor's expected schedule lines up with reality.
"""
import os
import sys
import time

# Make both `app` (backend/) and the repo root importable regardless of cwd —
# same reason as worker.py: no conftest / blueprint side effect in this process.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from sentry_sdk.crons import MonitorStatus, capture_checkin  # noqa: E402

from app import create_app  # noqa: E402
from app.services.zep_graph_sweeper import (  # noqa: E402
    sweep_dry_run,
    sweep_interval_minutes,
    sweep_max_deletes,
    sweep_orphan_graphs,
    sweep_page_size,
    sweep_ttl_hours,
)

SWEEP_MONITOR_SLUG = "zep-graph-sweep"
# Upper bound on a single sweep run (matches the old per-occurrence job timeout);
# feeds the Sentry monitor's max_runtime.
SWEEP_MAX_RUNTIME_MINUTES = 5


def _monitor_config() -> dict:
    """Sentry Cron ``monitor_config`` so a MISSED run alerts. Interval schedule
    from ``ZEP_SWEEP_INTERVAL_MINUTES`` (must match the Railway cron cadence), a
    one-interval ``checkin_margin`` grace, and a ``max_runtime`` bound."""
    interval = sweep_interval_minutes()
    return {
        "schedule": {"type": "interval", "value": interval, "unit": "minute"},
        "checkin_margin": interval,
        "max_runtime": SWEEP_MAX_RUNTIME_MINUTES,
        "timezone": "UTC",
    }


def main() -> int:
    # create_app() runs the issue-#6 boot gate (fail-fast on missing
    # SECRET_KEY/LLM_API_KEY/ZEP_API_KEY) and installs the PII-scrubbing Sentry
    # hooks (issue #17) — the same setup the web/worker entrypoints rely on.
    create_app()

    # Wrap the single sweep in a Sentry Cron Monitor check-in (in_progress → ok on
    # success, error on raise). A missed cron run surfaces as a missed check-in
    # raised by Sentry's own infrastructure — detected outside this process.
    monitor_config = _monitor_config()
    check_in_id = capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        status=MonitorStatus.IN_PROGRESS,
        monitor_config=monitor_config,
    )
    started = time.monotonic()
    try:
        sweep_orphan_graphs(
            dry_run=sweep_dry_run(),
            ttl_hours=sweep_ttl_hours(),
            page_size=sweep_page_size(),
            max_deletes=sweep_max_deletes(),
        )
    except Exception:
        capture_checkin(
            monitor_slug=SWEEP_MONITOR_SLUG,
            check_in_id=check_in_id,
            status=MonitorStatus.ERROR,
            duration=time.monotonic() - started,
            monitor_config=monitor_config,
        )
        # Re-raise so the process exits non-zero and Railway marks the cron run
        # failed (per-graph delete failures are counted inside the sweep and do
        # NOT raise; only a sweep-level failure, e.g. the listing itself, does).
        raise
    capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        check_in_id=check_in_id,
        status=MonitorStatus.OK,
        duration=time.monotonic() - started,
        monitor_config=monitor_config,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
