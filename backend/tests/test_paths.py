"""Unit tests for the path-safety primitives (issues #13, #24).

Pure functions, pinned here in isolation:

- safe_join: realpath containment — the real security boundary (#13).
- validate_merchant_id: fail-fast UUID format check for the X-Merchant-Id
  header at the request boundary (#24).

(The route-boundary validate_id gate was removed with the OASIS endpoints —
see the #24 prune.)
"""
import os

import pytest

from app.utils.paths import (
    InvalidID,
    PathTraversal,
    SENTINEL_MERCHANT_ID,
    safe_join,
    validate_merchant_id,
)


# --- validate_merchant_id (#24) ----------------------------------------------

def test_validate_merchant_id_accepts_canonical_uuid():
    # Returns the value unchanged so the before_request can bind it inline.
    value = "550e8400-e29b-41d4-a716-446655440000"
    assert validate_merchant_id(value) == value


def test_validate_merchant_id_accepts_sentinel():
    # The nil-UUID sentinel (un-headered/legacy requests + pre-multi-tenancy
    # data at rest) must pass the same validator so it flows through safe_join
    # like a real merchant id.
    assert validate_merchant_id(SENTINEL_MERCHANT_ID) == SENTINEL_MERCHANT_ID


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "550e8400e29b41d4a716446655440000",        # no hyphens
        "550e8400-e29b-41d4-a716-44665544000",     # 11 in last group — too short
        "550e8400-e29b-41d4-a716-4466554400000",   # 13 in last group — too long
        "550E8400-E29B-41D4-A716-446655440000",    # uppercase hex rejected (path determinism)
        "550e8400-e29b-41d4-a716-44665544zzzz",    # non-hex chars
        "../../../etc/passwd",
        "550e8400-e29b-41d4-a716-446655440000/../x",   # path payload after a valid uuid
        "../550e8400-e29b-41d4-a716-446655440000",     # traversal prefix
        "550e8400-e29b-41d4-a716-446655440000\n",      # trailing newline ($ would accept; \Z rejects)
        "550e8400-e29b-41d4-a716-446655440000\nx",     # embedded newline
    ],
)
def test_validate_merchant_id_rejects_malformed(value):
    with pytest.raises(InvalidID):
        validate_merchant_id(value)


def test_validate_merchant_id_rejects_non_string():
    with pytest.raises(InvalidID):
        validate_merchant_id(None)
    with pytest.raises(InvalidID):
        validate_merchant_id(12345)


# --- safe_join ----------------------------------------------------------------

def test_safe_join_returns_contained_path(tmp_path):
    base = str(tmp_path)
    result = safe_join(base, "proj_0123456789ab", "project.json")
    assert result == os.path.join(os.path.realpath(base), "proj_0123456789ab", "project.json")


def test_safe_join_no_parts_returns_base(tmp_path):
    base = str(tmp_path)
    assert safe_join(base) == os.path.realpath(base)


def test_safe_join_rejects_parent_traversal(tmp_path):
    base = str(tmp_path / "projects")
    os.makedirs(base)
    with pytest.raises(PathTraversal):
        safe_join(base, "../../etc/passwd")


def test_safe_join_rejects_dotdot_segment(tmp_path):
    base = str(tmp_path / "projects")
    os.makedirs(base)
    with pytest.raises(PathTraversal):
        safe_join(base, "..", "secret")


def test_safe_join_rejects_absolute_component(tmp_path):
    # os.path.join silently discards the base when a later component is
    # absolute — safe_join must catch this rather than return "/etc/passwd".
    base = str(tmp_path / "projects")
    os.makedirs(base)
    with pytest.raises(PathTraversal):
        safe_join(base, "/etc/passwd")


def test_safe_join_rejects_symlink_escape(tmp_path):
    # A symlink inside base pointing outside must not be a hole: realpath
    # resolves it, and the resolved target is outside base.
    base = tmp_path / "base"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    os.symlink(str(outside), str(base / "link"))
    with pytest.raises(PathTraversal):
        safe_join(str(base), "link", "secret.txt")


def test_safe_join_allows_nested_subdirs(tmp_path):
    base = str(tmp_path)
    result = safe_join(base, "sim_0123456789ab", "twitter", "actions.jsonl")
    assert result.startswith(os.path.realpath(base) + os.sep)
    assert result.endswith(os.path.join("twitter", "actions.jsonl"))
