"""Filesystem-path safety helpers (issue #13).

Two independent guards against an `<id>` URL/body param being joined into a
filesystem path with no containment check (Flask's default `<string:>`
converter accepts `..` and `/`, so a raw id can carry a traversal payload):

- ``validate_id(value, prefix)`` — fail-fast format check at the route
  boundary. This is the UX/fail-fast layer that yields a clean 400.
- ``safe_join(base, *parts)`` — the real security boundary. Resolves the
  joined path with ``realpath`` and rejects anything that escapes ``base``
  (via ``..``, an absolute-path component, or a symlink). Defense-in-depth so
  a site that forgets ``validate_id`` still cannot read or write outside its
  base directory.
"""
import os
import re


class InvalidID(ValueError):
    """A path-bearing id failed format validation (issue #13)."""


class PathTraversal(ValueError):
    """A join attempted to escape its base directory (issue #13)."""


# Route-param name -> id prefix. The three ids that are joined to filesystem
# paths are all minted as ``f"<prefix>_{uuid.uuid4().hex[:12]}"``:
#   project_id    -> proj_    (app/models/project.py)
#   simulation_id -> sim_     (app/services/simulation_manager.py)
#   report_id     -> report_  (app/api/report.py, app/services/report_agent.py)
# task_id (a bare uuid kept in an in-memory dict) and graph_id (a Zep-only
# graph key) never touch the filesystem and are deliberately absent — adding
# them would 400 every legitimate task/graph route.
ID_PARAM_PREFIXES = {
    "project_id": "proj",
    "simulation_id": "sim",
    "report_id": "report",
}

# Exactly the minted shape: prefix + 12 lowercase hex chars. \A/\Z (not ^/$)
# so a trailing newline can't slip through — $ also matches just before a final
# "\n". Intentionally strict so a generator drift (e.g. a bump to hex[:16])
# fails loudly in the round-trip tests rather than silently widening the surface.
_ID_RE = re.compile(r"\A(?:proj|sim|report)_[a-f0-9]{12}\Z")


def validate_id(value, prefix):
    """Return ``value`` if it is a well-formed id of ``prefix``, else raise.

    ``prefix`` is one of ``'proj'`` | ``'sim'`` | ``'report'``. A well-formed
    id of a *different* kind is rejected too, so a project route accepts only
    ``proj_`` ids, never a valid ``sim_``/``report_`` id.
    """
    if (
        not isinstance(value, str)
        or not value.startswith(prefix + "_")
        or not _ID_RE.match(value)
    ):
        raise InvalidID(f"invalid {prefix} id")
    return value


def safe_join(base, *parts):
    """Join ``parts`` onto ``base`` and return the result only if it stays
    inside ``base``; raise :class:`PathTraversal` otherwise.

    Guards against ``..`` traversal, absolute-path components (``os.path.join``
    silently discards ``base`` if a later part is absolute), and symlinks that
    resolve outside ``base`` (``realpath`` follows them).
    """
    base_real = os.path.realpath(base)
    target = os.path.realpath(os.path.join(base_real, *parts))
    if target != base_real and not target.startswith(base_real + os.sep):
        raise PathTraversal(f"path escapes base directory: {parts!r}")
    return target
