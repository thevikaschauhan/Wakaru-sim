"""Pytest fixtures for the backend test suite."""
import logging
import sys
from pathlib import Path

import pytest

# Ensure `from app import create_app` works regardless of pytest's cwd.
_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

# cart_recovery.py self-installs the repo root onto sys.path at module
# import (see backend/app/api/cart_recovery.py:15-19), but that runs only
# when create_app() registers the blueprint. Inserting here too keeps
# import order from breaking if a test imports cart_recovery.* directly
# before the Flask app fixture runs.
_REPO_ROOT = _BACKEND_DIR.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from app import create_app  # noqa: E402


# Shared test secret for the issue-#10 X-API-Key guard. The value is arbitrary;
# only that the autouse env and the `client` fixture agree on it.
TEST_WAKARU_API_KEY = "test-wakaru-api-key"


@pytest.fixture(autouse=True)
def config_env(monkeypatch):
    """Set required env vars so create_app()'s validate() (issues #6, #10) passes
    by default. Individual tests can override with monkeypatch.delenv /
    monkeypatch.setenv before calling create_app() themselves.

    Autouse: applies to every test in this directory. No leading underscore
    because autouse fixtures are public-by-contract."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("ZEP_API_KEY", "test")
    monkeypatch.setenv("WAKARU_API_KEY", TEST_WAKARU_API_KEY)


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    # Issue #12: disable rate limiting by default so the broad suite isn't
    # throttled (every test shares the test client's IP). The dedicated
    # rate-limit tests build their own app with limiting enabled.
    app.config["RATELIMIT_ENABLED"] = False

    # caplog attaches its handler to the root logger and relies on
    # propagation. The mirofish logger sets propagate=False in production
    # (see app/utils/logger.py) so we flip it here to make records visible
    # to the test, restoring on teardown.
    mirofish_logger = logging.getLogger("mirofish")
    original_propagate = mirofish_logger.propagate
    mirofish_logger.propagate = True
    yield app
    mirofish_logger.propagate = original_propagate


@pytest.fixture
def client(app):
    c = app.test_client()
    # Authenticate every request by default (issue #10) so existing /api/* tests
    # need no changes. Tests that want an unauthenticated request build their own
    # client from the `app` fixture.
    c.environ_base["HTTP_X_API_KEY"] = TEST_WAKARU_API_KEY
    return c
