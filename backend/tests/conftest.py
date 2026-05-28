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


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True

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
    return app.test_client()
