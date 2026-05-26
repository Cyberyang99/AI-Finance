"""fa chat — 自然语言对话入口."""

__all__ = ["run_repl"]


def __getattr__(name):
    # 延迟 import 避免循环依赖（resolver 不依赖 repl）
    if name == "run_repl":
        from .repl import run_repl
        return run_repl
    raise AttributeError(name)
