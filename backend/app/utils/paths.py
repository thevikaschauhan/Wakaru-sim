"""Filesystem-path safety helpers (issues #13, #24).

Guards against an untrusted id being joined into a filesystem path with no
containment check (Flask's default `<string:>` converter accepts `..` and `/`,
so a raw value can carry a traversal payload):

- ``safe_join(base, *parts)`` — the real security boundary. Resolves the joined
  path with ``realpath`` and rejects anything that escapes ``base`` (via ``..``,
  an absolute-path component, or a symlink). The in-process cart-recovery
  pipeline writes its project/simulation/report artifacts under ``uploads/``
  through this.
- ``validate_merchant_id(value)`` (issue #24) — fail-fast format check for the
  ``X-Merchant-Id`` header at the request boundary: merchant_id is path-bearing
  untrusted input, so it is validated to a path-safe UUID before any join,
  yielding a clean 400.

The earlier route-boundary ``validate_id`` / ``ID_PARAM_PREFIXES`` gate was
removed with the OASIS ``/graph``,``/simulation``,``/report`` endpoints (#24
prune) — no remaining route carries those id params.
"""
import os
import re


class InvalidID(ValueError):
    """A path-bearing id failed format validation (issue #13)."""


class PathTraversal(ValueError):
    """A join attempted to escape its base directory (issue #13)."""


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
