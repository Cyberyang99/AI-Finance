"""配置管理 — 读取 config.toml + .env."""

import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent


def load_config():
    import toml

    cfg_path = PROJECT_DIR / "config.toml"
    if not cfg_path.exists():
        example = PROJECT_DIR / "config.toml.example"
        if example.exists():
            print(f"[INIT] 首次运行，请从 config.toml.example 复制配置: cp {example} {cfg_path}")
        return {}
    return toml.load(open(cfg_path, encoding="utf-8"))


def load_env():
    """从 .env 加载环境变量 (.env 优先级最高，强制覆盖 shell env)。

    场景: Claude Code 注入的 ANTHROPIC_BASE_URL=https://api.anthropic.com 会
    覆盖我们 .env 里的 deepseek URL。.env 是项目级明确意图，应高于 shell。
    """
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    # utf-8-sig 自动剥离 BOM (Windows PowerShell Set-Content -Encoding utf8 会写 BOM)
    for line in open(env_path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if v:  # 仅写非空值
            os.environ[k] = v


load_env()

# macOS Python 3.14 SSL 证书修复 — 无条件覆盖系统错误路径
try:
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()
except ImportError:
    pass

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
# ANTHROPIC_AUTH_TOKEN: 仅当代理明确要求 Bearer (Authorization header) 时才使用
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "")
EODHD_KEY = os.environ.get("EODHD_API_KEY", "")
# 自定义 base_url 支持（用于 DeepSeek 等兼容 Anthropic API 的代理）
# DeepSeek: https://api.deepseek.com/anthropic
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "") or None

# DeepSeek 官方文档：x-api-key Fully Supported → 默认走 api_key (x-api-key header)
# 仅当显式设置 ANTHROPIC_USE_AUTH_TOKEN=1 且有 token 时才走 Bearer
USE_AUTH_TOKEN = (
    os.environ.get("ANTHROPIC_USE_AUTH_TOKEN", "").lower() in ("1", "true", "yes")
    and bool(ANTHROPIC_AUTH_TOKEN)
)

if not ANTHROPIC_KEY and not ANTHROPIC_AUTH_TOKEN:
    print("[WARN] ANTHROPIC_API_KEY 未设置，Agent 需要它调用 LLM")


def make_anthropic_client():
    """统一构造 Anthropic 客户端。

    默认: api_key → x-api-key header (DeepSeek 和官方 Anthropic 都支持)。
    设置 ANTHROPIC_USE_AUTH_TOKEN=1 时改走 Bearer (auth_token)。
    """
    import anthropic
    # DeepSeek 网关偶发瞬断；report 是 5 连环调用，一跳失败全程白跑，重试给足
    kwargs = {"max_retries": 4}
    if ANTHROPIC_BASE_URL:
        kwargs["base_url"] = ANTHROPIC_BASE_URL
    if USE_AUTH_TOKEN:
        kwargs["auth_token"] = ANTHROPIC_AUTH_TOKEN
    else:
        kwargs["api_key"] = ANTHROPIC_KEY or ANTHROPIC_AUTH_TOKEN
    return anthropic.Anthropic(**kwargs)
