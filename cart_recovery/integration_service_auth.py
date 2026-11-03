"""C11 bounded service grants. Wire-compatible with Engine integrationauth.

Exact requests can replay for 30 seconds; endpoints must remain idempotent.
Keys are provisioned per tenant/store/audience, independently of merchant JWTs.
"""
import hashlib
import hmac
import re
import time
from uuid import UUID

MAX_BODY = 200000


class ServiceGrants:
    def __init__(self, audience, grants):
        self.audience = audience
        self.grants = {}
        identifier = r"[a-zA-Z0-9_-]{1,64}"
        if not re.fullmatch(identifier, audience) or not 1 <= len(grants) <= 1000:
            raise ValueError("integration_grants_invalid")
        for value in grants:
            g = dict(value)
            if (set(g) != {"key_id", "secret", "merchant_id", "store_id", "audience", "operations"}
                    or not re.fullmatch(identifier, g["key_id"])
                    or g["key_id"] in self.grants
                    or not 32 <= len(g["secret"].encode()) <= 256
                    or str(UUID(g["merchant_id"])) != g["merchant_id"]
                    or not re.fullmatch(r"[1-9][0-9]{0,9}", g["store_id"])
                    or g["audience"] != audience or not 1 <= len(g["operations"]) <= 16
                    or any(not op.startswith("POST /internal/integrations/") or any(c in op for c in "?\r\n#*") for op in g["operations"])):
                raise ValueError("integration_grants_invalid")
            g["operations"] = tuple(g["operations"])
            self.grants[g["key_id"]] = g

    def verify(self, authorization, method, path, body, now=None):
        try:
            if len(body) > MAX_BODY or len(authorization) > 512 or not authorization.startswith("Integration v1."):
                return None
            kid, stamp, signature = authorization[len("Integration v1."):].split(".")
            g = self.grants[kid]
            now = int(time.time() if now is None else now)
            at = int(stamp)
            if str(at) != stamp or at > now or at < now - 30 or method + " " + path not in g["operations"]:
                return None
            payload = "\n".join(("integration_service_request_v1", kid, g["audience"], g["merchant_id"], g["store_id"], method, path, stamp, hashlib.sha256(body).hexdigest()))
            expected = hmac.new(g["secret"].encode(), payload.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                return None
            return {k: g[k] for k in ("merchant_id", "store_id", "audience", "key_id")}
        except (KeyError, ValueError, TypeError, UnicodeError):
            return None
