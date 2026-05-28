"""Issue #6 — Config.validate() must raise loud at create_app() boot.

Covers AC items 1 (refuse boot without SECRET_KEY), 2 (FLASK_DEBUG defaults
False), 3 (Config.validate() runs at boot and collects errors), and 5 (boot-
time failure mode has a test). The conftest autouse fixture sets baseline
env vars; each test overrides via monkeypatch.
"""
import pytest

from app import create_app


def test_create_app_raises_when_secret_key_missing(monkeypatch):
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        create_app()


def test_create_app_raises_when_secret_key_is_literal_default(monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "mirofish-secret-key")
    with pytest.raises(RuntimeError, match="mirofish-secret-key"):
        create_app()


def test_create_app_boots_with_valid_secret_key_and_debug_defaults_false():
    # SECRET_KEY/LLM_API_KEY/ZEP_API_KEY come from the conftest autouse
    # fixture. FLASK_DEBUG is not set, so DEBUG must default to False.
    app = create_app()
    assert app is not None
    assert app.config["DEBUG"] is False
