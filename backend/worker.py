"""RQ worker entrypoint for async cart-recovery jobs (issue #20).

Runs as a SEPARATE Railway service off the same image (start command:
``python worker.py`` from ``/app/backend``). It must NOT inherit the web
service's ``healthcheckPath = /health`` — a bare RQ worker serves no HTTP and
would be marked unhealthy and restart-looped. Concurrency scales by worker
*replicas*, not gunicorn ``--workers``.
"""
import os
import sys

# Make both `app` (backend/) and `cart_recovery` (the repo root, one level up)
# importable regardless of cwd / PYTHONPATH. The job body imports
# `cart_recovery.shopify_formatter`; unlike the web tier, the worker has neither
# conftest nor the request path's blueprint-import side effect to add the root.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)
for _p in (_BACKEND_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app import create_app  # noqa: E402
from app.services.job_queue import ANALYZE_QUEUE_NAME, get_redis_connection  # noqa: E402
from app.services.maintenance_queue import (  # noqa: E402
    MAINTENANCE_QUEUE_NAME,
    on_work_horse_killed,
    reconcile_chain,
)

# Import the job bodies eagerly so a broken import fails the worker at startup
# (loud) rather than on the first dequeued job (silent), and so RQ's fork can
# resolve them by path.
from app.services.cart_recovery_jobs import run_analysis_job  # noqa: E402,F401
from app.services.maintenance_queue import run_sweep_occurrence  # noqa: E402,F401


def main() -> None:
    # create_app() runs the issue-#6 boot gate (validate() -> fail-fast on
    # missing SECRET_KEY/LLM_API_KEY/ZEP_API_KEY), installs the PII-scrubbing
    # Sentry hooks (issue #17), registers OASIS subprocess cleanup, and inserts
    # the repo root on sys.path (via the cart_recovery blueprint import) so the
    # job body's `from cart_recovery...` imports resolve in this process.
    create_app()

    connection = get_redis_connection()
    if connection is None:
        print(
            "ERROR: REDIS_URL is not configured — the cart-recovery worker "
            "cannot start.",
            file=sys.stderr,
        )
        sys.exit(1)

    from rq import Worker

    # Re-seed the recurring orphan-graph sweep chain if it is dead (issue #72,
    # maintenance_queue.reconcile_chain). Guarded so a Redis hiccup at boot can
    # never crash the worker — the Sentry Cron monitor catches a still-dead
    # chain independently.
    try:
        reconcile_chain(connection)
    except Exception:
        print(
            "WARNING: sweep-chain boot reconcile failed; the Sentry Cron "
            "monitor will surface a dead chain.",
            file=sys.stderr,
        )

    # create_app() above did the config/Sentry/cleanup setup; this process is a
    # non-HTTP RQ consumer, so we deliberately do NOT call app.run() (contrast
    # run.py's web entrypoint). It serves no HTTP — hence no /health (see docstring).
    # log_job_description=False: defense-in-depth so RQ never logs a job
    # description (the enqueue side already pins a PII-free one — issue #7).
    # analyze FIRST so a queued paid analysis always dequeues ahead of a sweep;
    # with_scheduler=True runs RQ's built-in scheduler for the sweep occurrences.
    # work_horse_killed_handler (maintenance_queue.on_work_horse_killed) re-seeds
    # the sweep chain AND frees the dead occurrence's stale lock on a hard horse
    # kill (revision-6 F3); it is a module-level function (not a closure) so the
    # real killed-horse path has a committed regression test (revision-8 F3), and
    # it acts only for a killed SWEEP occurrence (revision-8 F2).
    worker = Worker(
        [ANALYZE_QUEUE_NAME, MAINTENANCE_QUEUE_NAME],
        connection=connection,
        log_job_description=False,
        work_horse_killed_handler=on_work_horse_killed,
    )
    worker.work(with_scheduler=True)


if __name__ == "__main__":
    main()
