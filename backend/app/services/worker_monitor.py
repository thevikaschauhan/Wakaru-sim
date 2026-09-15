"""Emit an actionable alarm on queue health transitions from the worker process."""
import logging
import os
import threading

import redis
import sentry_sdk

from .queue_health import queue_health

logger = logging.getLogger("mirofish.cart_recovery")


def start_queue_monitor(interval=60):
    stop = threading.Event()

    def monitor():
        unhealthy = False
        # Startup grace lets RQ register before the first observation.
        while not stop.wait(interval):
            try:
                with redis.Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3, socket_timeout=3) as connection:
                    health = queue_health(connection)
                failed = not health["healthy"]
            except (redis.RedisError, ValueError, KeyError):
                health = {"healthy": False, "error": "queue_unavailable"}
                failed = True
            if failed and not unhealthy:
                logger.error("Analysis queue unhealthy: %s", health)
                sentry_sdk.capture_message("Analysis queue unhealthy: missing worker heartbeat or queued work older than five minutes", level="error")
            elif unhealthy and not failed:
                logger.info("Analysis queue recovered: %s", health)
            unhealthy = failed

    thread = threading.Thread(target=monitor, name="analysis-queue-monitor", daemon=True)
    thread.start()
    return stop
