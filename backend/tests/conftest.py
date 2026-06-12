"""Pytest fixtures for the backend test suite."""
import hashlib
import hmac
import io
import logging
import sys
import time
from pathlib import Path

import pytest
from flask.testing import FlaskClient

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

# Shared test secret for the issue-#11 HMAC body-signature guard on the
# cart-recovery POSTs. As above: arbitrary, but the autouse env and
# SigningFlaskClient must agree on it.
TEST_WAKARU_INTERNAL_SECRET = "test-wakaru-internal-secret"


class SigningFlaskClient(FlaskClient):
    """Test client that HMAC-signs POSTs to /api/cart-recovery/* (issue #11).

    The #10 X-API-Key could be injected as a static environ_base header; the
    #11 signature depends on each request's exact body bytes, so it must be
    computed per request. Signing here lets the pre-#11 cart-recovery tests
    keep passing unchanged, exactly as the `client` fixture's environ_base
    kept the pre-#10 tests passing. Tests that need a missing/wrong/expired
    signature build a plain client via create_app() + app.test_client()
    (see test_cart_recovery_hmac.py).
    """

    def open(self, *args, **kwargs):
        if args and isinstance(args[0], str) and args[0].startswith("/api/cart-recovery") \
                and kwargs.get("method") == "POST":
            # buffered/follow_redirects belong to open(), not to the builder
            # (Flask's own open() consumes them via its signature).
            buffered = kwargs.pop("buffered", False)
            follow_redirects = kwargs.pop("follow_redirects", False)
            # Materialize the request exactly as Flask would (same JSON
            # serializer, same environ_base merge), then sign the final bytes.
            request = self._request_from_builder_args(args, kwargs)
            if "HTTP_X_WAKARU_SIGNATURE" not in request.environ:
                body = request.environ["wsgi.input"].read()
                # Restore the consumed stream so the app reads the same bytes.
                request.environ["wsgi.input"] = io.BytesIO(body)
                ts = str(int(time.time()))
                sig = hmac.new(
                    TEST_WAKARU_INTERNAL_SECRET.encode(),
                    f"{ts}.".encode() + body,
                    hashlib.sha256,
                ).hexdigest()
                request.environ["HTTP_X_WAKARU_TIMESTAMP"] = ts
                request.environ["HTTP_X_WAKARU_SIGNATURE"] = sig
            return super().open(
                request, buffered=buffered, follow_redirects=follow_redirects
            )
        return super().open(*args, **kwargs)


@pytest.fixture(autouse=True)
def config_env(monkeypatch):
    """Set required env vars so create_app()'s validate() (issues #6, #10, #11)
    passes by default. Individual tests can override with monkeypatch.delenv /
    monkeypatch.setenv before calling create_app() themselves.

    Autouse: applies to every test in this directory. No leading underscore
    because autouse fixtures are public-by-contract."""
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("LLM_API_KEY", "test")
    monkeypatch.setenv("ZEP_API_KEY", "test")
    monkeypatch.setenv("WAKARU_API_KEY", TEST_WAKARU_API_KEY)
    monkeypatch.setenv("WAKARU_INTERNAL_SECRET", TEST_WAKARU_INTERNAL_SECRET)


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    # Issue #11: sign cart-recovery POSTs by default so every pre-#11 test
    # exercises (rather than bypasses) the HMAC gate. See SigningFlaskClient.
    app.test_client_class = SigningFlaskClient
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
