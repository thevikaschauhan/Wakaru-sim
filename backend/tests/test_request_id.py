"""Issue #26 — X-Request-ID: inbound read (validated), response echo, Sentry tag.

AC: a caller can send X-Request-ID to correlate a request across services; an
invalid/oversized value falls back to a freshly minted 8-hex-char id (matching
the existing self-generated shape asserted elsewhere, e.g.
test_cart_recovery_pii.py); every response echoes back whichever id was used;
and the id is tagged onto the current Sentry scope.
"""
from unittest.mock import patch


def test_valid_inbound_request_id_is_honored_and_echoed(client):
    resp = client.get("/health", headers={"X-Request-ID": "deadbeefcafe0123"})
    assert resp.headers["X-Request-ID"] == "deadbeefcafe0123"


def test_valid_inbound_request_id_is_lowercased(client):
    resp = client.get("/health", headers={"X-Request-ID": "DEADBEEF"})
    assert resp.headers["X-Request-ID"] == "deadbeef"


def test_malformed_inbound_request_id_is_replaced_with_minted_id(client):
    # Contains a bracket — would corrupt the bracketed log-line shape
    # (`[{request_id} m={merchant_id}]`) if trusted verbatim.
    resp = client.get("/health", headers={"X-Request-ID": "abc]def"})
    minted = resp.headers["X-Request-ID"]
    assert minted != "abc]def"
    assert len(minted) == 8
    assert all(c in "0123456789abcdef" for c in minted)


def test_oversized_inbound_request_id_is_replaced_with_minted_id(client):
    resp = client.get("/health", headers={"X-Request-ID": "a" * 65})
    minted = resp.headers["X-Request-ID"]
    assert len(minted) == 8


def test_missing_inbound_request_id_mints_an_8_hex_char_id(client):
    resp = client.get("/health")
    minted = resp.headers["X-Request-ID"]
    assert len(minted) == 8
    assert all(c in "0123456789abcdef" for c in minted)


def test_response_always_carries_x_request_id_header(client):
    resp = client.get("/health")
    assert "X-Request-ID" in resp.headers


def test_sentry_tag_set_with_request_id(client):
    with patch("app.sentry_sdk.set_tag") as mock_set_tag:
        resp = client.get("/health", headers={"X-Request-ID": "deadbeefcafe0123"})
    mock_set_tag.assert_any_call("request_id", "deadbeefcafe0123")
    assert resp.status_code == 200
