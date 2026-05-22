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
    """从 .env 加载环境变量 (不覆盖已有)"""
    env_path = PROJECT_DIR / ".env"
    if not env_path.exists():
        return
    for line in open(env_path):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip()


load_env()

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
EODHD_KEY = os.environ.get("EODHD_API_KEY", "")

if not ANTHROPIC_KEY:
    print("[WARN] ANTHROPIC_API_KEY 未设置，Agent 需要它调用 Claude API")
