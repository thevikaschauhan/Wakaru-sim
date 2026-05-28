"""
MiroFish Backend - Flask应用工厂
"""

import os
import warnings
import sentry_sdk

# 抑制 multiprocessing resource_tracker 的警告（来自第三方库如 transformers）
# 需要在所有其他导入之前设置
warnings.filterwarnings("ignore", message=".*resource_tracker.*")

from flask import Flask, request
from flask_cors import CORS

from .config import Config
from .utils.logger import setup_logger, get_logger


# PII field names that must never leave the server (see issue #7).
_PII_FIELDS = frozenset({
    "email",
    "customer_name",
    "customer_id",
    "checkout_token",
    "payment_gateway_attempted",
    "location",
    "browsing_history",
})


def _scrub_pii(event, hint):
    """Strip PII from Sentry events before they leave the server."""
    if "request" in event:
        if "headers" in event["request"]:
            event["request"]["headers"] = {
                k: v for k, v in event["request"]["headers"].items()
                if k.lower() != "authorization"
            }
        data = event["request"].get("data")
        if isinstance(data, dict):
            event["request"]["data"] = {
                k: ("[redacted]" if k in _PII_FIELDS else v)
                for k, v in data.items()
            }
    return event


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
    
    # 启用CORS
    CORS(app, resources={r"/api/*": {"origins": "*"}})
    
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

