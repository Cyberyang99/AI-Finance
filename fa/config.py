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
    return toml.load(open(cfg_path))


def load_env():
    """从 .env 加载环境变量 (.env 优先级最高，强制覆盖 shell env)。

    场景: Claude Code 注入的 ANTHROPIC_BASE_URL=https://api.anthropic.com 会
    覆盖我们 .env 里的 deepseek URL。.env 是项目级明确意图，应高于 shell。
    """
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    for line in open(env_path):
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
# ANTHROPIC_AUTH_TOKEN 用于 Bearer 认证（DeepSeek 等代理需要）
ANTHROPIC_AUTH_TOKEN = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or ANTHROPIC_KEY
EODHD_KEY = os.environ.get("EODHD_API_KEY", "")
# 自定义 base_url 支持（用于 DeepSeek 等兼容 Anthropic API 的代理）
ANTHROPIC_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "") or None

# 当配置了 base_url（说明走代理），优先使用 auth_token 而非 api_key
USE_AUTH_TOKEN = bool(ANTHROPIC_BASE_URL)

if not ANTHROPIC_KEY and not ANTHROPIC_AUTH_TOKEN:
    print("[WARN] ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN 未设置，Agent 需要它调用 LLM")


def make_anthropic_client():
    """统一构造 Anthropic 客户端，自动处理 DeepSeek 等代理的认证差异。"""
    import anthropic
    if USE_AUTH_TOKEN:
        return anthropic.Anthropic(auth_token=ANTHROPIC_AUTH_TOKEN, base_url=ANTHROPIC_BASE_URL)
    return anthropic.Anthropic(api_key=ANTHROPIC_KEY)
