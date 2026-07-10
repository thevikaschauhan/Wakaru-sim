"""Issue #26: JSON logging, stdout-only, no rotating file handler."""
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from app.utils.logger import setup_logger


def test_logger_has_single_stdout_handler_no_file_handler():
    logger = setup_logger("test.mirofish.no_file_handler")
    assert len(logger.handlers) == 1
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    assert not isinstance(handler, RotatingFileHandler)
    assert handler.stream is sys.stdout


def test_json_log_line_has_required_fields(capsys):
    logger = setup_logger("test.mirofish.required_fields")
    logger.info("hello world")

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test.mirofish.required_fields"
    assert payload["message"] == "hello world"
    assert "timestamp" in payload
    assert "request_id" not in payload
    assert "merchant_id" not in payload
    assert "job_id" not in payload


def test_json_log_picks_up_request_id_and_merchant_id_from_flask_g(app, capsys):
    logger = setup_logger("test.mirofish.request_context")
    with app.test_request_context("/"):
        from flask import g
        g.request_id = "abcd1234"
        g.merchant_id = "11111111-1111-1111-1111-111111111111"
        logger.info("inside a request")

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["request_id"] == "abcd1234"
    assert payload["merchant_id"] == "11111111-1111-1111-1111-111111111111"


def test_json_log_picks_up_job_id_and_merchant_id_from_extra_outside_request_context(capsys):
    logger = setup_logger("test.mirofish.worker_extra")
    logger.error(
        "job failed",
        extra={"job_id": "job-abc-123", "merchant_id": "22222222-2222-2222-2222-222222222222"},
    )

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert payload["job_id"] == "job-abc-123"
    assert payload["merchant_id"] == "22222222-2222-2222-2222-222222222222"
    assert "request_id" not in payload


def test_json_log_includes_exc_info_on_exceptions(capsys):
    logger = setup_logger("test.mirofish.exc_info")
    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("unhandled", exc_info=True)

    line = capsys.readouterr().out.strip()
    payload = json.loads(line)
    assert "exc_info" in payload
    assert "ValueError: boom" in payload["exc_info"]
