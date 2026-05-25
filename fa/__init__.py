__version__ = "0.1.0"

# 触发 .env 加载 + SSL 证书修复 (config.py 顶层执行)
from . import config  # noqa: F401
