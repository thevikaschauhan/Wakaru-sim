"""Explicit C11 loopback process for cross-language HTTP acceptance only."""
import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
from werkzeug.serving import make_server
from .integration_service import create_service_app
from .integration_privacy import PrivacyStore


def main():
    parser = argparse.ArgumentParser()
    for name in ("merchant", "store", "secret", "ready"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    if not args.secret.startswith("c11-fixture-"):
        raise ValueError("synthetic key required")
    with TemporaryDirectory(prefix="c11-wakaru-") as directory:
        grant = dict(key_id="fixture", secret=args.secret, merchant_id=args.merchant, store_id=args.store, audience="wakaru", operations=["POST /internal/integrations/analyze"])
        app = create_service_app(enabled=True, grants=[grant], store=PrivacyStore(Path(directory) / "privacy.sqlite"))
        server = make_server("127.0.0.1", 0, app)
        Path(args.ready).write_text(f"http://127.0.0.1:{server.server_port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
