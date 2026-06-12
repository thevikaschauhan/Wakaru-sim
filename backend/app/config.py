"""
配置管理
统一从项目根目录的 .env 文件加载配置
"""

import os
from dotenv import load_dotenv

# 加载项目根目录的 .env 文件
# 路径: MiroFish/.env (相对于 backend/app/config.py)
project_root_env = os.path.join(os.path.dirname(__file__), '../../.env')

if os.path.exists(project_root_env):
    load_dotenv(project_root_env, override=True)
else:
    # 如果根目录没有 .env，尝试加载环境变量（用于生产环境）
    load_dotenv(override=True)


# Issue #6: SECRET_KEY must not equal this OSS-published literal default.
# Exported so tests can reference it without re-typing the string.
BANNED_SECRET_KEY_DEFAULT = 'mirofish-secret-key'

# Issue #10: WAKARU_API_KEY must not equal the .env.example placeholder (it is
# public, so booting with it would expose an enumerable shared secret).
BANNED_WAKARU_API_KEY_DEFAULT = 'your_wakaru_api_key_here'

# Issue #11: WAKARU_INTERNAL_SECRET must not equal the .env.example placeholder
# (it is public, so booting with it would let anyone forge request signatures).
BANNED_WAKARU_INTERNAL_SECRET_DEFAULT = 'your_wakaru_internal_secret_here'


class Config:
    """Flask配置类"""
    
    # Flask配置
    # SECRET_KEY 和 FLASK_DEBUG 默认值见 issue #6 — SECRET_KEY 在 validate() 中强制校验
    SECRET_KEY = os.environ.get('SECRET_KEY')
    DEBUG = os.environ.get('FLASK_DEBUG', 'False').lower() == 'true'
    
    # JSON配置 - 禁用ASCII转义，让中文直接显示（而不是 \uXXXX 格式）
    JSON_AS_ASCII = False
    
    # LLM配置（统一使用OpenAI格式）
    LLM_API_KEY = os.environ.get('LLM_API_KEY')
    LLM_BASE_URL = os.environ.get('LLM_BASE_URL', 'https://api.openai.com/v1')
    LLM_MODEL_NAME = os.environ.get('LLM_MODEL_NAME', 'gpt-4o-mini')
    
    # Zep配置
    ZEP_API_KEY = os.environ.get('ZEP_API_KEY')
    
    # 文件上传配置
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 50MB
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), '../uploads')
    ALLOWED_EXTENSIONS = {'pdf', 'md', 'txt', 'markdown'}
    
    # 文本处理配置
    DEFAULT_CHUNK_SIZE = 500  # 默认切块大小
    DEFAULT_CHUNK_OVERLAP = 50  # 默认重叠大小
    
    # OASIS模拟配置
    OASIS_DEFAULT_MAX_ROUNDS = int(os.environ.get('OASIS_DEFAULT_MAX_ROUNDS', '10'))
    OASIS_SIMULATION_DATA_DIR = os.path.join(os.path.dirname(__file__), '../uploads/simulations')
    
    # OASIS平台可用动作配置
    OASIS_TWITTER_ACTIONS = [
        'CREATE_POST', 'LIKE_POST', 'REPOST', 'FOLLOW', 'DO_NOTHING', 'QUOTE_POST'
    ]
    OASIS_REDDIT_ACTIONS = [
        'LIKE_POST', 'DISLIKE_POST', 'CREATE_POST', 'CREATE_COMMENT',
        'LIKE_COMMENT', 'DISLIKE_COMMENT', 'SEARCH_POSTS', 'SEARCH_USER',
        'TREND', 'REFRESH', 'DO_NOTHING', 'FOLLOW', 'MUTE'
    ]
    
    # Report Agent配置
    REPORT_AGENT_MAX_TOOL_CALLS = int(os.environ.get('REPORT_AGENT_MAX_TOOL_CALLS', '5'))
    REPORT_AGENT_MAX_REFLECTION_ROUNDS = int(os.environ.get('REPORT_AGENT_MAX_REFLECTION_ROUNDS', '2'))
    REPORT_AGENT_TEMPERATURE = float(os.environ.get('REPORT_AGENT_TEMPERATURE', '0.5'))
    
    @classmethod
    def validate(cls) -> list[str]:
        """验证必要配置 — 在 create_app() 启动时调用，缺失则 fail-fast.

        所有检查直接读 os.environ 而非 cls.<ATTR>: 类属性在 import 时固化,
        monkeypatch / 测试 / 运行时改动 env 需要 validate() 读取最新值.
        生产环境下两者等价 (env 已由 load_dotenv() 注入)."""
        errors: list[str] = []
        if not os.environ.get('LLM_API_KEY'):
            errors.append("LLM_API_KEY 未配置")
        if not os.environ.get('ZEP_API_KEY'):
            errors.append("ZEP_API_KEY 未配置")
        # Issue #10: the X-API-Key guard on /api/* fails closed at boot. An empty
        # key would also make hmac.compare_digest("", "") return True at request
        # time (auth bypass), so refuse to start without it. .strip() + placeholder
        # rejection mirror the SECRET_KEY defense above (a padded or copy-pasted
        # .env.example value must not satisfy the gate).
        wakaru_key = (os.environ.get('WAKARU_API_KEY') or '').strip()
        if not wakaru_key or wakaru_key == BANNED_WAKARU_API_KEY_DEFAULT:
            errors.append(
                f"WAKARU_API_KEY is not configured (required for /api/* auth; "
                f"issue #10; cannot use the placeholder '{BANNED_WAKARU_API_KEY_DEFAULT}')"
            )
        # Issue #11: the HMAC body-signature guard on the cart-recovery POSTs
        # fails closed at request time, but refuse to boot without the secret so
        # a missing var surfaces at deploy instead of as a 503 storm. Same
        # .strip() + placeholder rejection as WAKARU_API_KEY above. Required on
        # web AND worker (both boot via create_app()); must match the engine's
        # WAKARU_INTERNAL_SECRET.
        internal_secret = (os.environ.get('WAKARU_INTERNAL_SECRET') or '').strip()
        if not internal_secret or internal_secret == BANNED_WAKARU_INTERNAL_SECRET_DEFAULT:
            errors.append(
                f"WAKARU_INTERNAL_SECRET is not configured (required for HMAC "
                f"verification on the cart-recovery POSTs; issue #11; cannot use "
                f"the placeholder '{BANNED_WAKARU_INTERNAL_SECRET_DEFAULT}')"
            )
        # .strip() defends against shell/dashboard inputs that wrap the
        # forbidden literal in whitespace (multi-agent review round 1).
        secret = (os.environ.get('SECRET_KEY') or '').strip()
        if not secret or secret == BANNED_SECRET_KEY_DEFAULT:
            errors.append(
                f"SECRET_KEY 未配置 (生产环境必需; "
                f"不能使用默认值 '{BANNED_SECRET_KEY_DEFAULT}')"
            )
        return errors

