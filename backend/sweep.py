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
deletion (OP1). The cadence is NOT env-configurable: Railway's ``cronSchedule``
is the only scheduler, so ``SWEEP_CRON_SCHEDULE`` below mirrors it verbatim and
``tests/test_sweep.py`` pins the two together (a Sentry monitor whose expected
schedule disagreed with the cron would alert on every healthy run).
"""
import os
import signal
import sys
import time

# Make both `app` (backend/) and the repo root importable regardless of cwd —
# same reason as worker.py: no conftest / blueprint side effect in this process.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import sentry_sdk  # noqa: E402
from sentry_sdk.crons import MonitorStatus, capture_checkin  # noqa: E402

from app import create_app  # noqa: E402
from app.services.zep_graph_sweeper import (  # noqa: E402
    sweep_dry_run,
    sweep_max_deletes,
    sweep_orphan_graphs,
    sweep_page_size,
    sweep_ttl_hours,
)

SWEEP_MONITOR_SLUG = "zep-graph-sweep"
# The ONLY source of the cadence in code: must equal railway.sweep.toml's
# `cronSchedule` (pinned by tests/test_sweep.py). Railway crons are UTC, which
# is why the monitor_config below declares timezone UTC.
SWEEP_CRON_SCHEDULE = "0 * * * *"
# Grace before Sentry calls a run missed: one full period, unchanged from the
# interval schedule this replaced. Railway does not guarantee minute-precision
# firing ("execution times can vary by a few minutes"), so a tight margin would
# alert on healthy runs.
SWEEP_CHECKIN_MARGIN_MINUTES = 60
# Hard upper bound on a single sweep run, ENFORCED by the SIGALRM watchdog in
# main() (carried over from the deleted RQ occurrence's job_timeout=300, whose
# death penalty used the same signal) and reported to Sentry as max_runtime.
# Without it a hung Zep/Redis call would keep this process Active forever, and
# Railway SKIPS a scheduled run while the previous one is still running — i.e.
# one hang would silently end all future sweeps.
SWEEP_MAX_RUNTIME_SECONDS = 300
# This process exits seconds after its check-in, so the envelope has to be pushed
# out explicitly: Sentry's atexit flush allows only shutdown_timeout (2s), and a
# check-in dropped on the way out reads as a MISSED run even though the sweep ran.
SENTRY_FLUSH_TIMEOUT_SECONDS = 10


class SweepTimeout(Exception):
    """The sweep exceeded ``SWEEP_MAX_RUNTIME_SECONDS`` and was cut short."""


def _monitor_config() -> dict:
    """Sentry Cron ``monitor_config`` so a MISSED run alerts. The crontab
    schedule mirrors the Railway cron verbatim, plus a ``checkin_margin`` grace
    and the enforced ``max_runtime`` bound (ceil to whole minutes, the unit
    Sentry takes)."""
    return {
        "schedule": {"type": "crontab", "value": SWEEP_CRON_SCHEDULE},
        "checkin_margin": SWEEP_CHECKIN_MARGIN_MINUTES,
        "max_runtime": (SWEEP_MAX_RUNTIME_SECONDS + 59) // 60,
        "timezone": "UTC",
    }


def _arm_runtime_bound() -> None:
    """Arm the SIGALRM watchdog. Raises ``SweepTimeout`` in the main thread if the
    sweep outlives its bound, which lands in main()'s except path (ERROR check-in
    + non-zero exit) so the NEXT cron run is not skipped. Safe to install here:
    ``create_app``'s cleanup only claims SIGTERM/SIGINT/SIGHUP."""
    def _on_alarm(signum, frame):
        raise SweepTimeout(
            f"sweep exceeded {SWEEP_MAX_RUNTIME_SECONDS}s; aborting so the next "
            f"cron run is not skipped"
        )

    signal.signal(signal.SIGALRM, _on_alarm)
    signal.alarm(SWEEP_MAX_RUNTIME_SECONDS)


def main() -> int:
    # create_app() runs the issue-#6 boot gate — fail-fast on missing SECRET_KEY,
    # LLM_API_KEY, ZEP_API_KEY, WAKARU_API_KEY and WAKARU_INTERNAL_SECRET (all
    # five, even though this process serves no HTTP: Config.validate() is
    # entrypoint-agnostic, so the cron service must carry them too — see
    # railway.sweep.toml) — and installs the PII-scrubbing Sentry hooks (issue
    # #17), the same setup the web/worker entrypoints rely on. A failure here
    # exits non-zero BEFORE any check-in, so it surfaces as a failed Railway run
    # plus a missed Sentry check-in.
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
    _arm_runtime_bound()
    try:
        sweep_orphan_graphs(
            dry_run=sweep_dry_run(),
            ttl_hours=sweep_ttl_hours(),
            page_size=sweep_page_size(),
            max_deletes=sweep_max_deletes(),
        )
    except Exception:
        # Disarm FIRST so a firing alarm cannot interrupt the ERROR check-in.
        signal.alarm(0)
        capture_checkin(
            monitor_slug=SWEEP_MONITOR_SLUG,
            check_in_id=check_in_id,
            status=MonitorStatus.ERROR,
            duration=time.monotonic() - started,
            monitor_config=monitor_config,
        )
        sentry_sdk.flush(timeout=SENTRY_FLUSH_TIMEOUT_SECONDS)
        # Re-raise so the process exits non-zero and Railway marks the cron run
        # failed (per-graph delete failures are counted inside the sweep and do
        # NOT raise; only a sweep-level failure, e.g. the listing itself, does).
        raise
    signal.alarm(0)
    capture_checkin(
        monitor_slug=SWEEP_MONITOR_SLUG,
        check_in_id=check_in_id,
        status=MonitorStatus.OK,
        duration=time.monotonic() - started,
        monitor_config=monitor_config,
    )
    sentry_sdk.flush(timeout=SENTRY_FLUSH_TIMEOUT_SECONDS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
