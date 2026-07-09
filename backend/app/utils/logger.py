"""
Logging configuration module.
Emits single-line JSON to stdout only (Railway captures stdout; a rotating
file handler would write to the container's ephemeral filesystem, which is
wiped on every restart and never read).
"""

import json
import logging
import sys

from flask import g, has_request_context


def _ensure_utf8_stdout():
    """
    Ensure stdout/stderr use UTF-8 encoding.
    Fixes garbled non-ASCII output in the Windows console.
    """
    if sys.platform == 'win32':
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')


class JsonFormatter(logging.Formatter):
    """Renders each LogRecord as one JSON line.

    request_id/merchant_id come from `extra=` on the log call when present,
    else from flask.g inside an active request context (the web process). The
    worker process has no request context, so call sites there (e.g.
    cart_recovery_jobs.py) always pass request_id/merchant_id/job_id via
    `extra=`."""

    def format(self, record):
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        request_id = getattr(record, "request_id", None)
        merchant_id = getattr(record, "merchant_id", None)
        job_id = getattr(record, "job_id", None)

        if (request_id is None or merchant_id is None) and has_request_context():
            request_id = request_id if request_id is not None else getattr(g, "request_id", None)
            merchant_id = merchant_id if merchant_id is not None else getattr(g, "merchant_id", None)

        if request_id is not None:
            payload["request_id"] = request_id
        if merchant_id is not None:
            payload["merchant_id"] = merchant_id
        if job_id is not None:
            payload["job_id"] = job_id

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False)


def setup_logger(name: str = 'mirofish', level: int = logging.DEBUG) -> logging.Logger:
    """
    Set up a logger.

    Args:
        name: logger name
        level: logging level

    Returns:
        the configured logger
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Don't propagate to the root logger — avoids duplicate output.
    logger.propagate = False

    # Already configured — don't add handlers twice.
    if logger.handlers:
        return logger

    _ensure_utf8_stdout()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(JsonFormatter())

    logger.addHandler(console_handler)

    return logger


def get_logger(name: str = 'mirofish') -> logging.Logger:
    """
    Get a logger (creating it if it doesn't exist).

    Args:
        name: logger name

    Returns:
        the logger instance
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        return setup_logger(name)
    return logger


# Default logger
logger = setup_logger()


# Convenience methods
def debug(msg, *args, **kwargs):
    logger.debug(msg, *args, **kwargs)

def info(msg, *args, **kwargs):
    logger.info(msg, *args, **kwargs)

def warning(msg, *args, **kwargs):
    logger.warning(msg, *args, **kwargs)

def error(msg, *args, **kwargs):
    logger.error(msg, *args, **kwargs)

def critical(msg, *args, **kwargs):
    logger.critical(msg, *args, **kwargs)
