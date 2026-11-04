"""Explicit C16 loopback client for enrolled legacy stores. No environment discovery."""

import hashlib
import hmac
import http.client
import json
import time
from urllib.parse import urlsplit

from .integration_service_auth import MAX_BODY, ServiceGrants


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

    def request(self, merchant, kind, key, body=None, *, admit=False):
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
            if reply.status != 200:
                raise ValueError("lineage_unavailable")
            data = reply.read(400001)
            if len(data) > 400000:
                raise ValueError("lineage_response_too_large")
            result = json.loads(data)
        finally:
            conn.close()
        if admit:
            if result != {"admitted": True}:
                raise ValueError("lineage_admission_denied")
        elif body is not None:
            if result != {"stored": True}:
                raise ValueError("lineage_capture_unconfirmed")
        elif set(result) == {"body"} and (
            isinstance(result["body"], str)
            or (kind == "plan" and result["body"] is None)
        ):
            return result["body"]
        else:
            raise ValueError("lineage_read_unavailable")

    def load(self, merchant, revision):
        body = self.request(merchant, "plan", revision)
        return json.loads(body) if body is not None else None

    def select(self, merchant, revision, intelligence, plan):
        self.request(
            merchant,
            "plan",
            revision,
            json.dumps([intelligence, plan], allow_nan=False),
        )
        value = self.load(merchant, revision)
        if value is None:
            raise ValueError("plan_unavailable")
        return value
