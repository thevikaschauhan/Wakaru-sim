"""Issue #13 — filesystem path-traversal containment (static guard).

The route-level before_request gate (Layer 1 of the original #13 design) was
removed with the OASIS /graph,/simulation,/report endpoints (#24 prune) — no
remaining route carries a project_id/simulation_id/report_id param. The real
security boundary, safe_join, stays: the in-process cart-recovery pipeline writes
its project/simulation/report artifacts under uploads/ through it.

This module keeps the static guard that pins that no service module joins a known
base dir with an untrusted id without routing through safe_join, plus the
read-vs-create discipline on the simulation dir. The pure primitives are
unit-tested in tests/test_paths.py.
"""
import os
import re
from pathlib import Path


_BACKEND = Path(__file__).resolve().parent.parent


# --- static guard — every base-dir id join goes through safe_join -------------

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
    joined = "\n".join((_BACKEND / rel).read_text(encoding="utf-8") for rel in _GUARDED_FILES)
    for token in ("RUN_STATE_DIR", "PROJECTS_DIR", "REPORTS_DIR"):
        assert token in joined, token


# --- read-only paths must not create directories ------------------------------

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
