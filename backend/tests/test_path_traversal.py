"""Issue #13 — route-level path-traversal containment.

Two layers are exercised here (the pure primitives are unit-tested in
tests/test_paths.py):

1. The app-level before_request gate turns a malformed path-bearing id
   (project_id / simulation_id / report_id) into a fail-fast 400 before any
   handler runs. Params that never touch the filesystem (task_id, graph_id)
   are deliberately *not* gated.
2. A static guard pins that no API/service module joins a known base dir with
   an untrusted id without routing through safe_join.

Routing note: werkzeug rejects an encoded multi-segment payload
(`..%2F..%2Fetc`) with a 404 at URL-matching time — it never reaches a route,
so it never reaches the gate. That is still a rejection (no file is read); the
gate's job is the single-segment ids that *do* reach a route
(`sim_....`, `notanid`). The genuinely exploitable vector — a traversal id in a
POST body, which carries no werkzeug normalization — is closed by safe_join at
the filesystem layer.
"""
import os
import re
from pathlib import Path

from app.utils.paths import ID_PARAM_PREFIXES


_BACKEND = Path(__file__).resolve().parent.parent


# --- Layer 1: the before_request 400 gate ------------------------------------

# Single-segment ids that reach a route and must be rejected by the gate.
MALFORMED_ID_ROUTES = [
    "/api/simulation/notanid/run-status",
    "/api/simulation/sim_xyz/run-status",        # right prefix, bad body
    "/api/simulation/sim_0123456789ab_extra/run-status",
    "/api/report/notareport",
    "/api/report/report_ZZZZ/progress",          # non-hex
    "/api/graph/project/notaproj",
    "/api/graph/project/proj_0123456789",         # 10 hex — too short
]


def test_malformed_url_id_is_rejected_with_400(client):
    for url in MALFORMED_ID_ROUTES:
        resp = client.get(url)
        assert resp.status_code == 400, (url, resp.status_code, resp.data[:120])
        assert resp.get_json()["error"] == "invalid_id", url


def test_valid_url_id_passes_the_gate(client):
    # A well-formed (if nonexistent) simulation id reaches the handler, which
    # reports "idle" — proving the gate let it through rather than 400ing it.
    resp = client.get("/api/simulation/sim_0123456789ab/run-status")
    assert resp.status_code == 200, resp.data[:200]
    assert resp.get_json()["data"]["runner_status"] == "idle"


def test_non_fs_params_are_not_gated(client):
    # task_id (bare uuid, in-memory) must not be 400'd as an invalid id — the
    # handler answers "not found" instead. graph_id (Zep-only) likewise is never
    # rejected by the id gate.
    resp = client.get("/api/graph/task/550e8400-e29b-41d4-a716-446655440000")
    assert resp.status_code != 400 or resp.get_json().get("error") != "invalid_id"


def test_encoded_multisegment_traversal_does_not_leak(client):
    # werkzeug 404s the encoded-slash payload before routing; the point is only
    # that it never returns a 200 with file contents.
    resp = client.get("/api/simulation/..%2F..%2F..%2Fetc%2Fpasswd/run-status")
    assert resp.status_code == 404


def test_empty_segment_id_does_not_reach_fs_as_200(client):
    # A bare ".." normalises away at routing; assert no successful read.
    resp = client.get("/api/simulation/../run-status")
    assert resp.status_code != 200


# --- Layer 2: static guard — every base-dir id join goes through safe_join ----

# Base-dir constants whose first-level joins carry an id taken (directly or
# transitively) from a request. Each `os.path.join(<BASE>, <id>...)` must be a
# safe_join instead.
_BASE_DIR_TOKENS = [
    "PROJECTS_DIR",
    "REPORTS_DIR",
    "RUN_STATE_DIR",
    "SIMULATION_DATA_DIR",
    "OASIS_SIMULATION_DATA_DIR",
]

# Files audited for issue #13. If a new module starts joining one of the base
# dirs with an id, add it here and route it through safe_join.
_GUARDED_FILES = [
    "app/models/project.py",
    "app/services/report_agent.py",
    "app/services/simulation_manager.py",
    "app/services/simulation_runner.py",
    "app/api/simulation.py",
    "app/services/zep_tools.py",
]


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def test_no_unguarded_base_dir_id_join():
    """Grep-assert: no `os.path.join(<BASE_DIR>, ...)` remains in the guarded
    files. First-level id joins must use safe_join(<BASE_DIR>, id, ...).

    Scans the full text (not line-by-line) so a multi-line os.path.join with
    the base token on the next line is still caught — `\\s*` matches newlines.
    """
    pattern = re.compile(
        r"os\.path\.join\(\s*(?:self\.|cls\.|Config\.)?(" + "|".join(_BASE_DIR_TOKENS) + r")\b"
    )
    offenders = []
    for rel in _GUARDED_FILES:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            offenders.append(f"{rel}:{_line_of(text, m.start())}: {m.group(0)}")
    assert not offenders, (
        "base-dir id joins must use safe_join, not os.path.join:\n"
        + "\n".join(offenders)
    )


def test_no_fstring_id_path_embedding():
    """Grep-assert: no id is interpolated into a path-shaped f-string literal
    (e.g. f'../../uploads/simulations/{simulation_id}'). os.path.join can't
    contain such a string, so these must be rewritten as safe_join(base, id).
    Requires a '/' before the id placeholder so error/log f-strings that merely
    mention an id are not flagged."""
    pattern = re.compile(
        r"f['\"][^'\"]*/\{(?:simulation_id|project_id|report_id|sim_id)\}"
    )
    offenders = []
    for rel in _GUARDED_FILES:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        for m in pattern.finditer(text):
            offenders.append(f"{rel}:{_line_of(text, m.start())}: {m.group(0)}")
    assert not offenders, (
        "id-in-path f-strings must be rewritten as safe_join(base, id):\n"
        + "\n".join(offenders)
    )


def test_guarded_files_cover_known_base_dirs():
    # Sanity: the base-dir tokens we guard actually still exist in the tree, so
    # a rename can't silently empty the guard.
    assert ID_PARAM_PREFIXES  # imported symbol is used
    joined = "\n".join((_BACKEND / rel).read_text(encoding="utf-8") for rel in _GUARDED_FILES)
    for token in ("RUN_STATE_DIR", "PROJECTS_DIR", "REPORTS_DIR"):
        assert token in joined, token


# --- AC item 3: read-only paths must not create directories ------------------

def test_read_only_get_simulation_dir_does_not_create(tmp_path, monkeypatch):
    # A read (create=False, the default) must not materialize the directory —
    # otherwise a garbage/traversal id would create attacker-named dirs before
    # any existence check. create=True is the explicit write path.
    from app.services.simulation_manager import SimulationManager

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path / "sims"))
    mgr = SimulationManager()
    sid = "sim_0123456789ab"

    read_dir = mgr._get_simulation_dir(sid)
    assert not os.path.exists(read_dir)

    write_dir = mgr._get_simulation_dir(sid, create=True)
    assert os.path.isdir(write_dir)
