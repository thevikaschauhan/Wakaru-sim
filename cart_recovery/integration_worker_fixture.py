"""Explicit loopback C13 test host. Never imported by production bootstrap."""
import argparse
import tempfile
from pathlib import Path
from werkzeug.serving import make_server
from .integration_privacy import PrivacyStore
from .integration_worker_health import create_worker_service_app
from .integration_invalidation import PATH


def main():
    parser = argparse.ArgumentParser()
    for name in ("merchant", "store", "secret", "ready"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if not args.secret.startswith("c13-fixture-"):
        raise ValueError("synthetic_key_required")
    grant = dict(key_id="fixture", secret=args.secret, merchant_id=args.merchant, store_id=args.store, audience="wakaru", operations=["POST /internal/integrations/analyze", "POST " + PATH, "POST /internal/integrations/worker-health"])
    with tempfile.TemporaryDirectory() as directory:
        app = create_worker_service_app(enabled=True, grants=[grant], store=PrivacyStore(Path(directory) / "fixture.sqlite"))
        server = make_server("127.0.0.1", 0, app)
        Path(args.ready).write_text("http://127.0.0.1:" + str(server.server_port))
        server.serve_forever()


if __name__ == "__main__":
    main()
