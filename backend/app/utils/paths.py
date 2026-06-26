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

``validate_merchant_id(value)`` (issue #24) is the same fail-fast layer for the
``X-Merchant-Id`` header: merchant_id becomes the top-level storage namespace
(``uploads/{merchant_id}/...``), so it is path-bearing untrusted input too and
must be validated to a path-safe UUID before any join.
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

# Multi-tenancy (#24): merchant_id arrives in the X-Merchant-Id header as the
# engine's merchant UUID (uuid.UUID rendered as its canonical lowercase string)
# and becomes a filesystem path component (uploads/{merchant_id}/...), so — like
# the ids above — it is untrusted input crossing into a path join (the #13
# hazard). A canonical UUID is hex + hyphen only, so a valid merchant_id can
# never carry '.', '/', or a '..' traversal payload. \A/\Z (not ^/$) so a
# trailing newline cannot slip through.
_MERCHANT_ID_RE = re.compile(
    r"\A[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z"
)

# The nil UUID, used as the merchant_id when a request carries no X-Merchant-Id
# (the legacy /analyze caller, or the engine before it sends the header) and as
# the migration sentinel for pre-multi-tenancy data at rest. It satisfies
# validate_merchant_id, so the sentinel flows through the same path-safe machinery
# as a real merchant id.
SENTINEL_MERCHANT_ID = "00000000-0000-0000-0000-000000000000"


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


def validate_merchant_id(value):
    """Return ``value`` if it is a canonical lowercase UUID, else raise.

    merchant_id is path-bearing untrusted input (it becomes the top-level
    storage namespace, ``uploads/{merchant_id}/...``), so it is validated at the
    request boundary exactly like the route ids — a non-UUID is rejected before
    it can reach a filesystem join. Raises :class:`InvalidID` so the route can
    reuse the issue-#13 fail-fast 400 path.
    """
    if not isinstance(value, str) or not _MERCHANT_ID_RE.match(value):
        raise InvalidID("invalid merchant id")
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
