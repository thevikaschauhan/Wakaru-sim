"""Issue #6 — Config.validate() must raise loud at create_app() boot.

Covers AC items 1 (refuse boot without SECRET_KEY), 2 (FLASK_DEBUG defaults
False), 3 (Config.validate() runs at boot and collects errors), and 5 (boot-
time failure mode has a test). The conftest autouse fixture sets baseline
env vars; each test overrides via monkeypatch.
"""
import pytest

from app import create_app
from app.config import BANNED_SECRET_KEY_DEFAULT


def test_create_app_raises_when_secret_key_missing(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


def test_create_app_raises_when_secret_key_is_literal_default(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", BANNED_SECRET_KEY_DEFAULT)
    with pytest.raises(RuntimeError, match=BANNED_SECRET_KEY_DEFAULT):
        create_app()


def test_create_app_raises_when_secret_key_is_padded_literal(monkeypatch):
    # Whitespace-wrapped literal must also be rejected (multi-agent review
    # round 1). Catches dashboard/shell inputs that accidentally pad values.
    monkeypatch.setenv("SECRET_KEY", f"  {BANNED_SECRET_KEY_DEFAULT}  ")
    with pytest.raises(RuntimeError, match=BANNED_SECRET_KEY_DEFAULT):
        create_app()


def test_create_app_raises_with_all_three_errors_joined(monkeypatch):
    # Exercises the collect-and-join path: all three required vars missing
    # must produce a single RuntimeError naming all three (review round 1).
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("ZEP_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as exc:
        create_app()
    msg = str(exc.value)
    assert "SECRET_KEY" in msg
    assert "LLM_API_KEY" in msg
    assert "ZEP_API_KEY" in msg


def test_flask_debug_defaults_false(app):
    # Uses the conftest `app` fixture so blueprint registration and teardown
    # are handled. FLASK_DEBUG is not set by the autouse env fixture, so the
    # default in config.py must take effect.
    assert app.config["DEBUG"] is False
