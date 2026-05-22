"""投资框架 — 加载/读取/建议更新 memory/framework/ 下的规则文件.

框架是 Agent 的分析检查清单，不是硬编码阈值。
每次分析时 Agent 读框架做指导，分析后可以根据反馈建议调整。
"""

from pathlib import Path

FRAMEWORK_DIR = Path(__file__).resolve().parent.parent / "memory" / "framework"

FRAMEWORK_FILES = {
    "quality": "checklist.md",      # 商业模式质量检查清单
    "red_flags": "red-flags.md",    # 风险信号库
    "valuation": "valuation.md",    # 估值方法论
}


def load_framework() -> dict[str, str]:
    """加载全部框架文件内容。返回 {name: content}."""
    result = {}
    for name, fname in FRAMEWORK_FILES.items():
        path = FRAMEWORK_DIR / fname
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
    return result


def get_framework_prompt() -> str:
    """生成注入 Agent 系统提示的框架文本."""
    frameworks = load_framework()
    if not frameworks:
        return ""

    parts = []
    for name, content in frameworks.items():
        if name == "quality":
            parts.append(f"## 商业模式质量检查清单\n\n{content}")
        elif name == "red_flags":
            parts.append(f"## 风险信号库（发现任一信号，必须在结论中讨论）\n\n{content}")
        elif name == "valuation":
            parts.append(f"## 估值方法论\n\n{content}")
    return "\n\n---\n\n".join(parts)


def save_framework(name: str, content: str):
    """更新框架文件。name ∈ {quality, red_flags, valuation}."""
    if name not in FRAMEWORK_FILES:
        raise ValueError(f"未知框架: {name}")
    path = FRAMEWORK_DIR / FRAMEWORK_FILES[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def suggest_framework_update(feedback: str) -> dict:
    """基于用户反馈，建议框架更新。

    Agent 调用此函数，传入用户纠正意见，返回建议的框架修改。
    实际修改需要用户确认。
    """
    return {
        "feedback": feedback,
        "suggestion": f"基于反馈 '{feedback}'，建议在对应框架文件中补充或修改条目。",
        "action": "请确认是否执行此框架更新",
    }
