"""C10 loopback-only fixture host. Never imported by production bootstrap.

Uses the real private C8 blueprint and SQLite privacy owner. Explicit synthetic
merchant/token arguments are required. No provider, model, Redis or worker.
"""
import argparse
import hmac
from pathlib import Path
from tempfile import TemporaryDirectory
from flask import Flask
from werkzeug.serving import make_server
from .bounded_context_api import create_blueprint
from .intelligence.bounded_context import BoundedService
from .integration_privacy import PrivacyStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--merchant", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()
    if not args.token.startswith("c10-fixture-"):
        raise ValueError("synthetic fixture token required")
    with TemporaryDirectory(prefix="c10-wakaru-") as directory:
        app = Flask(__name__)
        service = BoundedService(PrivacyStore(str(Path(directory) / "privacy.sqlite")))
        def authorize(request):
            return args.merchant if hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + args.token) else None
        app.register_blueprint(create_blueprint(service, authorize))
        server = make_server("127.0.0.1", 0, app)
        Path(args.ready).write_text(f"http://127.0.0.1:{server.server_port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
