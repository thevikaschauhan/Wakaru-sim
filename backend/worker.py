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

# Import the job body eagerly so a broken import fails the worker at startup
# (loud) rather than on the first dequeued job (silent), and so RQ's fork can
# resolve it by path.
from app.services.cart_recovery_jobs import run_analysis_job  # noqa: E402,F401


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

    # create_app() above did the config/Sentry/cleanup setup; this process is a
    # non-HTTP RQ consumer, so we deliberately do NOT call app.run() (contrast
    # run.py's web entrypoint). It serves no HTTP — hence no /health (see docstring).
    worker = Worker([ANALYZE_QUEUE_NAME], connection=connection)
    worker.work(with_scheduler=False)


if __name__ == "__main__":
    main()
