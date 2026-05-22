"""投资框架管理 — 加载/读取/更新 memory/framework/ 下的规则文件.

L1 硬框架: 人工编辑，Agent 只读。
L2 软知识: 通过 memory/store.py 的 SectorKnowledge 管理。
L3 情景记忆: 通过 memory/store.py 的 Theses/Reviews 管理。
"""

from pathlib import Path

FRAMEWORK_DIR = Path(__file__).resolve().parent.parent / "memory" / "framework"

FRAMEWORK_FILES = {
    "quality": "checklist.md",
    "red_flags": "red-flags.md",
    "valuation": "valuation.md",
}


def load_framework() -> dict[str, str]:
    result = {}
    for name, fname in FRAMEWORK_FILES.items():
        path = FRAMEWORK_DIR / fname
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
    return result


def get_framework_prompt() -> str:
    """生成完整框架文本（deep 模式注入）。"""
    frameworks = load_framework()
    if not frameworks:
        return ""

    parts = []
    labels = {
        "quality": "## 商业模式质量检查清单\n\n",
        "red_flags": "## 风险信号库\n\n",
        "valuation": "## 估值方法论\n\n",
    }
    for name, content in frameworks.items():
        label = labels.get(name, f"## {name}\n\n")
        parts.append(label + content)

    return "\n\n---\n\n".join(parts)


def save_framework(name: str, content: str):
    if name not in FRAMEWORK_FILES:
        raise ValueError(f"未知框架: {name}, 可选: {list(FRAMEWORK_FILES.keys())}")
    path = FRAMEWORK_DIR / FRAMEWORK_FILES[name]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
