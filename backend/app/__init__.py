"""
MiroFish Backend - Flask应用工厂
"""

import hmac
import os
import traceback
import uuid
import warnings
import sentry_sdk

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, g, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

from .config import Config
from .extensions import limiter
from .utils.logger import setup_logger, get_logger
from .utils.paths import (
    ID_PARAM_PREFIXES,
    InvalidID,
    SENTINEL_MERCHANT_ID,
    validate_id,
    validate_merchant_id,
)


# PII field names that must never leave the server (see issues #7, #17).
_PII_FIELDS = frozenset({
    "email",
    "customer_name",
    "customer_id",
    "checkout_token",
    "payment_gateway_attempted",
    "location",
    "browsing_history",
    "products_viewed",
    "searches_submitted",
})


# Cap recursion so a hostile deeply-nested body cannot exhaust the Python
# stack inside the Sentry hook (review: DoS class). Cart payloads are 2-3
# levels deep; 25 is far beyond any legitimate structure.
_MAX_REDACT_DEPTH = 25


# Request headers that carry credentials and must never reach Sentry (compared
# case-insensitively). X-API-Key is the issue-#10 shared secret on every /api/*
# request; Authorization is the long-standing bearer header.
_SENSITIVE_HEADERS = frozenset({"authorization", "x-api-key"})


def _redact_pii(obj, _depth=0):
    """Recursively replace PII-keyed values with a redaction marker.

    Cart payloads nest PII inside lists/dicts (browsing_history,
    products_viewed, ...), so a shallow top-level pass is not enough
    (issue #17). Non-container values pass through unchanged. Anything deeper
    than _MAX_REDACT_DEPTH is redacted wholesale rather than risk a stack
    overflow on hostile input.

    NOTE: redaction is by dict key, so a raw-string body (e.g. form-encoded)
    passes through unchanged — safe today because Sentry body capture is off
    (max_request_body_size='never')."""
    if _depth >= _MAX_REDACT_DEPTH:
        return "[redacted:depth]"
    if isinstance(obj, dict):
        return {
            k: ("[redacted]" if k in _PII_FIELDS else _redact_pii(v, _depth + 1))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_redact_pii(item, _depth + 1) for item in obj]
    return obj


def _scrub_pii(event, hint):
    """Strip PII from Sentry events before they leave the server (issue #17).

    Sentry's before_send hook: drops credential headers (Authorization,
    X-API-Key) and recursively redacts PII in the request body."""
    if "request" in event:
        if "headers" in event["request"]:
            event["request"]["headers"] = {
                k: v for k, v in event["request"]["headers"].items()
                if k.lower() not in _SENSITIVE_HEADERS
            }
        if "data" in event["request"]:
            event["request"]["data"] = _redact_pii(event["request"]["data"])
    return event


def _scrub_breadcrumb(crumb, hint):
    """Sentry's before_breadcrumb hook: recursively redact PII in breadcrumb
    data so log/HTTP breadcrumbs cannot carry cart PII into an event (#17)."""
    if isinstance(crumb, dict) and "data" in crumb:
        crumb["data"] = _redact_pii(crumb["data"])
    return crumb


def create_app(config_class=Config):
    """Flask应用工厂函数"""
    # Fail fast on missing required env vars before any Flask/Sentry setup.
    # See issue #6 — Config.validate() is the boot gate for SECRET_KEY,
    # LLM_API_KEY, and ZEP_API_KEY.
    config_errors = config_class.validate()
    if config_errors:
        raise RuntimeError(
            "Cannot start: missing required configuration:\n  - "
            + "\n  - ".join(config_errors)
        )

    # Initialize Sentry error monitoring (no-op when DSN is empty)
    sentry_dsn = os.environ.get("SENTRY_DSN", "")
    if sentry_dsn:
        sentry_sdk.init(
            dsn=sentry_dsn,
            environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
            release="wakaru@1.0.0",
            traces_sample_rate=0.0,
            send_default_pii=False,
            # Tell Sentry not to capture request bodies. _scrub_pii still
            # redacts dict bodies as belt-and-suspenders if a future change
            # re-enables body capture.
            max_request_body_size="never",
            before_send=_scrub_pii,
            before_breadcrumb=_scrub_breadcrumb,
        )

    app = Flask(__name__)
    app.config.from_object(config_class)
    
    # 设置JSON编码：确保中文直接显示（而不是 \uXXXX 格式）
    # Flask >= 2.3 使用 app.json.ensure_ascii，旧版本使用 JSON_AS_ASCII 配置
    if hasattr(app, 'json') and hasattr(app.json, 'ensure_ascii'):
        app.json.ensure_ascii = False
    
    # 设置日志
    logger = setup_logger('mirofish')
    
    # 只在 reloader 子进程中打印启动信息（避免 debug 模式下打印两次）
    is_reloader_process = os.environ.get('WERKZEUG_RUN_MAIN') == 'true'
    debug_mode = app.config.get('DEBUG', False)
    should_log_startup = not debug_mode or is_reloader_process
    
    if should_log_startup:
        logger.info("=" * 50)
        logger.info("MiroFish Backend 启动中...")
        logger.info("=" * 50)
    
    # Issue #10: restrict CORS on /api/* to an explicit allowlist instead of the
    # former wildcard. ALLOWED_ORIGINS is a comma-separated list; empty = no
    # browser origin is permitted. The engine calls /api/* server→server, so the
    # default empty allowlist is correct; this repo ships no browser frontend.
    allowed_origins = [
        o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()
    ]
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

    # Issue #14: tag every request with a short correlation id so the opaque
    # error responses can be matched to their server-side log line. Registered
    # before require_api_key so even rejected requests are tagged. Supersedes the
    # per-handler request_id noted as a Phase-3 TODO in cart_recovery.
    @app.before_request
    def assign_request_id():
        g.request_id = uuid.uuid4().hex[:8]

    # Issue #10: single shared-secret X-API-Key guard on /api/*. The /api/ prefix
    # check leaves /health (not under /api/) open. OPTIONS preflight carries no
    # auth header by design, so it is exempted and answered by flask-cors. This
    # is an interim model; per-merchant keys land with multi-tenancy (#24).
    @app.before_request
    def require_api_key():
        if request.method == "OPTIONS" or not request.path.startswith("/api/"):
            return None
        expected = os.environ.get("WAKARU_API_KEY")
        if not expected:
            # Fail closed: an empty expected key makes hmac.compare_digest("", "")
            # return True (auth bypass). validate() blocks this at boot; this is
            # defense in depth for any path that bypasses the boot gate.
            return jsonify({"error": "server_auth_not_configured"}), 503
        provided = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(provided, expected):
            return jsonify({"error": "unauthorized"}), 401
        return None

    # Issue #13: reject a malformed project_id/simulation_id/report_id at the
    # route boundary before it can reach a filesystem join (Flask's <string:>
    # converter happily accepts "..", a dotted id, etc.). Keyed on the view_arg
    # NAME so it ignores task_id (a bare in-memory uuid) and graph_id (a Zep-only
    # key) without 400ing their routes. Registered after require_api_key so an
    # unauthenticated probe still 401s first. This is the fail-fast UX layer for
    # URL ids; safe_join at the filesystem layer is the real containment boundary
    # and additionally covers ids that arrive in a POST body.
    @app.before_request
    def validate_path_ids():
        for name, value in (request.view_args or {}).items():
            prefix = ID_PARAM_PREFIXES.get(name)
            if prefix is None:
                continue
            try:
                validate_id(value, prefix)
            except InvalidID:
                return jsonify({"error": "invalid_id"}), 400
        return None

    # Issue #24: bind the engine-supplied merchant to the request so storage,
    # rate limit, idempotency, and logs can be scoped per tenant. Registered
    # AFTER require_api_key (a key holder is authenticated first) and BEFORE
    # limiter.init_app below, so g.merchant_id is already set when the limiter's
    # per-merchant key_func runs. Scoped to /api/cart-recovery/* — the only live
    # multi-tenant surface (the engine is the sole server→server caller). A
    # request with no X-Merchant-Id (the legacy /analyze caller, or the engine
    # before it sends the header) is bucketed under the nil-UUID sentinel rather
    # than rejected, so this can deploy before the engine starts sending it; a
    # present-but-malformed id fails loud with 400, since the engine controls the
    # value so a bad one is a bug/attack, not a legacy caller. The id becomes a
    # filesystem path component (storage namespacing, #24 CP2), so it is validated
    # to a path-safe UUID here at the boundary — the issue-#13 hazard.
    @app.before_request
    def resolve_merchant_id():
        # Trailing slash so this matches only the blueprint's routes
        # (/api/cart-recovery/…), never a hypothetical sibling like
        # /api/cart-recoveryX.
        if not request.path.startswith("/api/cart-recovery/"):
            return None
        raw = request.headers.get("X-Merchant-Id")
        if not raw:
            g.merchant_id = SENTINEL_MERCHANT_ID
        else:
            try:
                g.merchant_id = validate_merchant_id(raw)
            except InvalidID:
                return jsonify({"error": "invalid_merchant_id"}), 400
        # merchant_id is a tenant UUID, not PII (it is absent from _PII_FIELDS),
        # so tagging Sentry with it is safe and lets errors be filtered per tenant.
        sentry_sdk.set_tag("merchant_id", g.merchant_id)
        return None

    # Issue #12: rate limit the paid cart-recovery POSTs. Init AFTER the auth
    # before_request above so a missing key still 401s before any 429. Use the
    # provisioned Redis for storage so the limit coheres across gunicorn workers
    # (an in-process counter would not); fall back to in-memory if REDIS_URL is
    # unset (the sync path still works — the limit is just per-process then).
    redis_url = os.environ.get("REDIS_URL")
    if redis_url:
        app.config["RATELIMIT_STORAGE_URI"] = redis_url
    elif should_log_startup:
        logger.warning(
            "REDIS_URL not set — rate limiting uses in-memory storage "
            "(per-process, not shared across workers)."
        )
    # Emit X-RateLimit-* on limited responses and a Retry-After on 429 so the
    # caller can back off correctly (issue #12 AC).
    app.config["RATELIMIT_HEADERS_ENABLED"] = True
    limiter.init_app(app)

    # 注册模拟进程清理函数（确保服务器关闭时终止所有模拟进程）
    from .services.simulation_runner import SimulationRunner
    SimulationRunner.register_cleanup()
    if should_log_startup:
        logger.info("已注册模拟进程清理函数")
    
    # 请求日志中间件
    @app.before_request
    def log_request():
        logger = get_logger('mirofish.request')
        logger.debug(f"请求: {request.method} {request.path}")
        # Request body intentionally not logged — see issue #7 (PII leak).
    
    @app.after_request
    def log_response(response):
        logger = get_logger('mirofish.request')
        logger.debug(f"响应: {response.status_code}")
        return response

    # Issue #16 — OWASP baseline security headers on every response.
    # No Content-Security-Policy: this service is a pure JSON API with no
    # browser frontend, so a CSP would have no document/scripts to govern.
    @app.after_request
    def set_security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        # HSTS only over HTTPS in production — never on local plain-HTTP dev.
        if os.environ.get("ENVIRONMENT") == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )
        return response

    # Issue #14: global catch-all so an unhandled exception returns an opaque
    # 500 instead of a stack trace — a stack trace leaks container paths, library
    # versions, and internal module layout. The full traceback is still written
    # to the server log (the file handler is DEBUG) for triage.
    @app.errorhandler(Exception)
    def handle_unexpected_error(error):
        # HTTPExceptions are deliberate control flow — the #10 auth 401, the #12
        # rate-limit 429 + Retry-After, 404s, etc. A bare Exception handler is
        # otherwise matched for them via the class hierarchy, so re-render them
        # unchanged; they are not info leaks.
        if isinstance(error, HTTPException):
            return error
        request_id = getattr(g, "request_id", "unknown")
        # Sanitized ERROR line only — the type name, never str(e) or exc_info.
        # The mirofish logger reaches Sentry via LoggingIntegration, and an
        # exception's .args / frame locals can carry PII (issues #7/#17). The
        # full traceback goes to DEBUG, which is below Sentry's event level but
        # still written to the log file.
        logger.error(f"[{request_id}] Unhandled exception ({type(error).__name__})")
        logger.debug(traceback.format_exc())
        sentry_sdk.capture_message(
            f"Unhandled exception ({type(error).__name__})", level="error"
        )
        return jsonify(
            {"success": False, "error": "internal_error", "request_id": request_id}
        ), 500

    # 注册蓝图
    from .api import graph_bp, simulation_bp, report_bp
    app.register_blueprint(graph_bp, url_prefix='/api/graph')
    app.register_blueprint(simulation_bp, url_prefix='/api/simulation')
    app.register_blueprint(report_bp, url_prefix='/api/report')

    # Vakaru cart recovery integration
    from .api.cart_recovery import cart_recovery_bp
    app.register_blueprint(cart_recovery_bp, url_prefix='/api/cart-recovery')

    # 健康检查
    @app.route('/health')
    def health():
        return {'status': 'ok', 'service': 'MiroFish Backend'}
    
    if should_log_startup:
        logger.info("MiroFish Backend 启动完成")
    
    return app

