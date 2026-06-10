"""投资框架管理 — 加载/读取/更新 memory/framework/ 下的规则文件.

L1 硬框架: 人工编辑，Agent 只读。
L2 软知识: 通过 memory/store.py 的 SectorKnowledge 管理。
L3 情景记忆: 通过 memory/store.py 的 Theses/Reviews 管理。

分析框架注册表（frameworks/ 子目录，fa report 路由用）:
  - 一个框架 = 一个 md 文件，frontmatter(name/title/description/applies/avoid) + 正文
  - 文件人写人改、可持续外挂：新增框架 = 扔一个 md 进目录，代码零改动
  - 路由的「闭合词表」就是注册表本身：LLM 只能从已存在的框架里选，
    选不出回退通用 12+3 维模板（general）
  - `_` 开头的文件跳过（草稿/停用约定）
"""

import re
from pathlib import Path
from typing import Optional

FRAMEWORK_DIR = Path(__file__).resolve().parent.parent / "memory" / "framework"
FRAMEWORKS_DIR = FRAMEWORK_DIR / "frameworks"
REVIEW_RULES_FILE = FRAMEWORK_DIR / "review-rules.md"

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


# ── 分析框架注册表 ──

def _parse_fw_file(path: Path) -> Optional[dict]:
    """解析单个框架文件。frontmatter 残缺时返回 None（调用方提示，不静默吞）。"""
    import yaml

    text = path.read_text(encoding="utf-8-sig")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return None
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return None
    if not meta.get("name"):
        return None
    return {
        "name": str(meta["name"]).strip(),
        "title": str(meta.get("title") or meta["name"]).strip(),
        "description": str(meta.get("description") or "").strip(),
        "applies": str(meta.get("applies") or "").strip(),
        "avoid": str(meta.get("avoid") or "").strip(),
        "body": text[m.end():].strip(),
        "path": str(path),
    }


def list_analysis_frameworks() -> list[dict]:
    """加载全部分析框架（路由候选集）。坏文件打印警告后跳过。"""
    if not FRAMEWORKS_DIR.exists():
        return []
    out = []
    for p in sorted(FRAMEWORKS_DIR.glob("*.md")):
        if p.name.startswith("_"):
            continue
        fw = _parse_fw_file(p)
        if fw is None:
            print(f"  [frameworks] ⚠ {p.name} frontmatter 残缺（需 name 字段），已跳过")
            continue
        out.append(fw)
    return out


def get_analysis_framework(name: str) -> Optional[dict]:
    for fw in list_analysis_frameworks():
        if fw["name"] == name:
            return fw
    return None


# ── 点评沉淀（review-rules）──

def load_review_rules() -> str:
    """读取用户点评沉淀的方法论规则（`## 规则` 之后的正文）。

    无文件 / 正文为空 / 以「（暂无」开头的占位 → 返回 ""，调用方跳过注入。
    """
    if not REVIEW_RULES_FILE.exists():
        return ""
    text = REVIEW_RULES_FILE.read_text(encoding="utf-8-sig")
    m = re.search(r"^## 规则\s*$(.*)", text, re.DOTALL | re.MULTILINE)
    body = (m.group(1) if m else text).strip()
    if not body or body.startswith("（暂无"):
        return ""
    return body


def inject_review_rules(system: str) -> str:
    """把点评规则拼到合成 system prompt 末尾（vet/report 共用）。无规则原样返回。"""
    rules = load_review_rules()
    if not rules:
        return system
    return system + "\n\n## 用户点评沉淀的方法论规则（长期偏好，必须遵守）\n" + rules
