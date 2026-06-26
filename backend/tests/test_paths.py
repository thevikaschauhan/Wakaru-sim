"""Issue #13 — unit tests for the path-safety primitives.

`validate_id` and `safe_join` are the two independent guards added by issue
#13. They are pure functions, so they are pinned here in isolation (the
route-level behaviour is covered by tests/test_path_traversal.py):

- validate_id: fail-fast format check at the route boundary (the UX layer).
- safe_join: realpath containment — the real security boundary.
"""
import os

import pytest

from app.utils.paths import (
    ID_PARAM_PREFIXES,
    InvalidID,
    PathTraversal,
    SENTINEL_MERCHANT_ID,
    safe_join,
    validate_id,
    validate_merchant_id,
)


# --- validate_id --------------------------------------------------------------

@pytest.mark.parametrize(
    "value,prefix",
    [
        ("proj_0123456789ab", "proj"),
        ("sim_abcdef012345", "sim"),
        ("report_deadbeef0011", "report"),
    ],
)
def test_validate_id_accepts_canonical_ids(value, prefix):
    # Returns the value unchanged so callers can validate inline.
    assert validate_id(value, prefix) == value


@pytest.mark.parametrize(
    "value",
    [
        "../../../etc/passwd",
        "proj_../../etc",
        "proj_/etc/passwd",
        "proj_..",
        "..",
        "",
        "proj_",                    # prefix only, no hex
        "proj_0123456789a",         # 11 hex — too short
        "proj_0123456789abc",       # 13 hex — too long
        "proj_0123456789AB",        # uppercase hex rejected
        "proj_0123456789zz",        # non-hex chars
        "PROJ_0123456789ab",        # uppercase prefix
        "proj0123456789ab",         # missing underscore
        "proj_0123456789ab\n",      # trailing newline ($ would accept; \\Z rejects)
        "proj_0123456789ab\nx",     # embedded newline
    ],
)
def test_validate_id_rejects_malformed(value):
    with pytest.raises(InvalidID):
        validate_id(value, "proj")


def test_validate_id_rejects_wrong_prefix_for_kind():
    # A well-formed id of the wrong kind must not pass on another kind's route:
    # a project route accepts only proj_ ids, never a valid sim_/report_ id.
    with pytest.raises(InvalidID):
        validate_id("sim_0123456789ab", "proj")
    with pytest.raises(InvalidID):
        validate_id("report_0123456789ab", "sim")


def test_validate_id_rejects_non_string():
    with pytest.raises(InvalidID):
        validate_id(None, "proj")
    with pytest.raises(InvalidID):
        validate_id(12345, "proj")


def test_id_param_prefixes_cover_exactly_the_fs_bearing_params():
    # task_id (bare uuid, in-memory) and graph_id (Zep-only) must stay absent —
    # adding them would 400 every legitimate task/graph route.
    assert ID_PARAM_PREFIXES == {
        "project_id": "proj",
        "simulation_id": "sim",
        "report_id": "report",
    }


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
