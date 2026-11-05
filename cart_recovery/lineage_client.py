"""Explicit C16 loopback client for enrolled legacy stores. No environment discovery."""

import hashlib
import hmac
import http.client
import json
import re
import time
from urllib.parse import urlsplit

from .integration_service_auth import MAX_BODY, ServiceGrants


class LineageConflict(ValueError):
    pass


class OfflineLineageClient:
    def __init__(self, origin, grant):
        url = urlsplit(origin)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or not url.port
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("offline_loopback_required")
        verifier = ServiceGrants("wakaru", [grant])
        self.grant = verifier.grants[grant["key_id"]]
        self.port = url.port

    def request(self, merchant, kind, key, body=None, *, admit=False, binding=None,
                attempt_id=None):
        grant = self.grant
        path = "/internal/integrations/lineage/" + (
            "admit" if admit else ("read" if body is None else "capture")
        )
        if (
            merchant != grant["merchant_id"]
            or "POST " + path not in grant["operations"]
        ):
            raise ValueError("lineage_unauthorized")
        value = {"merchant_id": merchant, "kind": kind, "key": key}
        if body is not None and binding is None:
            raise ValueError("lineage_admission_required")
        if binding is not None:
            value["binding"] = binding
        if attempt_id is not None:
            value["attempt_id"] = attempt_id
        if body is not None:
            value["body"] = body
        raw = json.dumps(value, allow_nan=False).encode()
        if len(raw) > MAX_BODY:
            raise ValueError("lineage_payload_too_large")
        stamp = str(int(time.time()))
        message = "\n".join(
            (
                "integration_service_request_v1",
                grant["key_id"],
                grant["audience"],
                merchant,
                grant["store_id"],
                "POST",
                path,
                stamp,
                hashlib.sha256(raw).hexdigest(),
            )
        )
        mac = hmac.new(
            grant["secret"].encode(), message.encode(), hashlib.sha256
        ).hexdigest()
        auth = f"Integration v1.{grant['key_id']}.{stamp}.{mac}"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        try:
            conn.request(
                "POST",
                path,
                raw,
                {"Authorization": auth, "Content-Type": "application/json"},
            )
            reply = conn.getresponse()
            if reply.status == 409:
                raise LineageConflict("lineage_unavailable")
            if reply.status != 200:
                raise ValueError("lineage_unavailable")
            data = reply.read(400001)
            if len(data) > 400000:
                raise ValueError("lineage_response_too_large")
            result = json.loads(data)
        finally:
            conn.close()
        if admit and attempt_id is not None:
            if result != {"admitted": True}:
                raise ValueError("lineage_admission_denied")
        elif admit or (kind == "plan" and body is None):
            result_binding = result.get("binding") if isinstance(result, dict) else None
            if (not isinstance(result_binding, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", result_binding)
                    or (binding is not None and result_binding != binding)):
                raise ValueError("lineage_admission_denied")
            if admit and result == {"admitted": True, "binding": result_binding}:
                return result_binding
            if not admit and set(result) == {"body", "binding"} and (
                result["body"] is None or isinstance(result["body"], str)
            ):
                return result["body"], result_binding
            raise ValueError("lineage_response_invalid")
        elif body is not None:
            if result != {"stored": True}:
                raise ValueError("lineage_capture_unconfirmed")
        elif set(result) == {"body"} and isinstance(result["body"], str):
            return result["body"]
        else:
            raise ValueError("lineage_read_unavailable")

    def lookup_plan(self, merchant, revision, *, binding=None):
        body, current = self.request(merchant, "plan", revision, binding=binding)
        return (json.loads(body) if body is not None else None), current

    def load(self, merchant, revision):
        value, _ = self.lookup_plan(merchant, revision)
        return value

    def select(self, merchant, revision, intelligence, plan, *, binding=None):
        try:
            self.request(
                merchant, "plan", revision,
                json.dumps([intelligence, plan], allow_nan=False),
                binding=binding,
            )
        except LineageConflict:
            # A competing producer may have selected a different plan since our miss.
            # Only a current authenticated read with the original binding can win.
            pass
        value, _ = self.lookup_plan(merchant, revision, binding=binding)
        if value is None:
            raise ValueError("plan_unavailable")
        return value
