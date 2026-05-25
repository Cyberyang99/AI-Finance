"""情境记忆系统 — YAML frontmatter + Markdown 笔记.

PDF 1 §2.2.3 设计:
  - 单条笔记 = YAML 元数据 + Markdown 正文 (与 Skill 同构)
  - MEMORY.md 全局索引（id + 简述）方便 LLM 全局召回
  - 召回方式: LLM 读索引直接判断 Top-K (起步阶段, 笔记 < 100 条最优)

笔记 schema (YAML frontmatter):
  id:                 唯一标识 (= 文件名 stem)
  situation:          一句话情境描述 (30-80 字)
  retrieval_text:     召回检索文本 (80-200 字, 含触发条件 + 适用范围)
  confidence:         0.0-1.0 笔记成熟度
  created_at:         创建日期 YYYY-MM-DD
  evolved_at:         最后修订日期
  source_thesis:      源论点 ticker (如 600519.SHG)
  source_excess_return: 源论点客观表现 %
  sector_scope:       适用行业 list (GICS 11 大类 + 'all')
  sector_excluded:    不适用行业 list
  refined_count:      被精炼次数
  validated_on:       list[ticker], 验证过此规律的标的
  absorbed:           bool, 是否已吸收到 system prompt (Phase 6+)
  archived:           bool, 归档则不召回

Markdown body 三段:
  ## 经验总结
  ## 建议调整
  ## 例外分支
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional


SITUATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "memory" / "situations"
INDEX_FILE = SITUATIONS_DIR / "MEMORY.md"
ARCHIVE_DIR = SITUATIONS_DIR / "_archive"

# YAML frontmatter 标准字段顺序（写盘时保持稳定顺序）
SCHEMA_FIELDS = [
    "id", "situation", "retrieval_text", "confidence",
    "created_at", "evolved_at",
    "source_thesis", "source_excess_return",
    "sector_scope", "sector_excluded",
    "validated_on", "refined_count",
    "absorbed", "archived",
]

# 默认值
SCHEMA_DEFAULTS = {
    "confidence": 0.5,
    "sector_scope": ["all"],
    "sector_excluded": [],
    "validated_on": [],
    "refined_count": 0,
    "absorbed": False,
    "archived": False,
}


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _slugify(text: str, max_len: int = 60) -> str:
    """从 situation 生成合法的文件名 id。"""
    text = re.sub(r"[^\w一-龥-]+", "_", text.strip().lower())
    text = re.sub(r"_+", "_", text).strip("_")
    return text[:max_len] or f"note_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


# ─────────────────────────────────────────────────────────────
# YAML/Markdown 解析 (不依赖 pyyaml, 手写避免新依赖)
# ─────────────────────────────────────────────────────────────

def _parse_yaml_value(s: str):
    """简易 YAML 值解析: 支持 str/int/float/bool/list."""
    s = s.strip()
    if not s:
        return ""
    # list: [a, b, c] 或 ['a', "b"]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        items = []
        for it in re.split(r",(?![^\[]*\])", inner):
            it = it.strip().strip("'\"")
            if it:
                items.append(it)
        return items
    # bool
    if s.lower() in ("true", "yes"):
        return True
    if s.lower() in ("false", "no"):
        return False
    # number
    try:
        if "." in s:
            return float(s)
        return int(s)
    except ValueError:
        pass
    # string (去引号)
    return s.strip("'\"")


def _format_yaml_value(v) -> str:
    """把值序列化回 YAML。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = ", ".join(f"'{x}'" if isinstance(x, str) else str(x) for x in v)
        return f"[{items}]"
    if v is None:
        return ""
    s = str(v)
    # 包含特殊字符的字符串加引号
    if any(c in s for c in [":", "#", "'", '"', "\n"]):
        return f'"{s.replace(chr(34), chr(92) + chr(34))}"'
    return s


def parse_note(text: str) -> dict:
    """解析单条笔记 (YAML frontmatter + markdown body)。

    返回 dict: {**frontmatter, "body": "markdown..."}.
    """
    if not text.startswith("---"):
        return {"body": text, "_invalid": True}

    parts = text.split("---", 2)
    if len(parts) < 3:
        return {"body": text, "_invalid": True}

    frontmatter_text = parts[1]
    body = parts[2].lstrip("\n")

    note = {}
    for line in frontmatter_text.split("\n"):
        line = line.rstrip()
        if not line.strip() or line.strip().startswith("#"):
            continue
        m = re.match(r"^\s*([\w_]+)\s*:\s*(.*)$", line)
        if m:
            note[m.group(1)] = _parse_yaml_value(m.group(2))

    note["body"] = body
    return note


def serialize_note(note: dict) -> str:
    """把 note dict 序列化回文件内容 (YAML frontmatter + body)."""
    lines = ["---"]
    for k in SCHEMA_FIELDS:
        if k in note:
            lines.append(f"{k}: {_format_yaml_value(note[k])}")
    # 用户加的额外字段
    for k, v in note.items():
        if k not in SCHEMA_FIELDS and k != "body":
            lines.append(f"{k}: {_format_yaml_value(v)}")
    lines.append("---")
    lines.append("")
    lines.append(note.get("body", "").lstrip("\n"))
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# SituationStore — 笔记 CRUD + 索引维护
# ─────────────────────────────────────────────────────────────

class SituationStore:
    """情境笔记的文件级 CRUD。"""

    def __init__(self, base_dir: Path = None):
        self.dir = base_dir or SITUATIONS_DIR
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "_archive").mkdir(exist_ok=True)
        self.index_path = self.dir / "MEMORY.md"

    # ── 单条 CRUD ──

    def path_of(self, note_id: str) -> Path:
        return self.dir / f"{note_id}.md"

    def exists(self, note_id: str) -> bool:
        return self.path_of(note_id).exists()

    def load(self, note_id: str) -> Optional[dict]:
        p = self.path_of(note_id)
        if not p.exists():
            return None
        return parse_note(p.read_text(encoding="utf-8"))

    def save(self, note: dict, rebuild_index: bool = True) -> str:
        """保存笔记，自动补默认字段。返回 note_id。"""
        # 必填字段
        if "situation" not in note or not note["situation"]:
            raise ValueError("note 必须有 situation 字段")

        # id 缺失则从 situation 生成
        if "id" not in note or not note["id"]:
            note["id"] = _slugify(note["situation"])

        # 默认值
        for k, v in SCHEMA_DEFAULTS.items():
            note.setdefault(k, v)
        note.setdefault("created_at", _today())
        note["evolved_at"] = _today()

        # body 默认模板
        if not note.get("body", "").strip():
            note["body"] = (
                "## 经验总结\n\n(待补充)\n\n"
                "## 建议调整\n\n- (待补充)\n\n"
                "## 例外分支\n\n(无)\n"
            )

        path = self.path_of(note["id"])
        path.write_text(serialize_note(note), encoding="utf-8")

        if rebuild_index:
            self.rebuild_index()
        return note["id"]

    def archive(self, note_id: str) -> bool:
        """归档笔记 (移动到 _archive/)。"""
        src = self.path_of(note_id)
        if not src.exists():
            return False
        dst = self.dir / "_archive" / src.name
        src.rename(dst)
        self.rebuild_index()
        return True

    def list_notes(self, include_archived: bool = False) -> list[dict]:
        """列出所有笔记的 frontmatter (不含 body)。"""
        notes = []
        for p in sorted(self.dir.glob("*.md")):
            if p.name in ("MEMORY.md", "README.md"):
                continue
            n = parse_note(p.read_text(encoding="utf-8"))
            if n.get("_invalid"):
                continue
            if n.get("archived") and not include_archived:
                continue
            n.pop("body", None)
            notes.append(n)

        if include_archived:
            for p in sorted((self.dir / "_archive").glob("*.md")):
                n = parse_note(p.read_text(encoding="utf-8"))
                if not n.get("_invalid"):
                    n["archived"] = True
                    n.pop("body", None)
                    notes.append(n)

        return notes

    # ── 索引 (MEMORY.md) ──

    def rebuild_index(self) -> str:
        """重建 MEMORY.md 索引。供 LLM 全局召回用。"""
        notes = self.list_notes(include_archived=False)
        lines = [
            "# 情境记忆索引 (MEMORY.md)",
            f"> 自动生成，共 {len(notes)} 条活跃笔记。手动编辑会被覆盖。",
            f"> 更新于: {_today()}",
            "",
        ]
        if not notes:
            lines.append("(暂无笔记)")
        else:
            lines.append("| ID | 情境 | 适用行业 | 置信度 | 创建日 |")
            lines.append("|----|------|---------|--------|--------|")
            for n in notes:
                sectors = ", ".join(n.get("sector_scope", ["all"]))
                conf = n.get("confidence", 0.5)
                lines.append(
                    f"| `{n['id']}` | {n.get('situation','')[:60]} | {sectors} | "
                    f"{conf} | {n.get('created_at','')} |"
                )

            # 详细召回区: id + retrieval_text + sector_scope (给 LLM 召回判断用)
            lines.append("")
            lines.append("## 召回检索区")
            lines.append("> 以下是每条笔记的检索文本，召回时基于此判断相关性。")
            lines.append("")
            for n in notes:
                lines.append(f"### `{n['id']}`")
                lines.append(f"- 情境: {n.get('situation', '')}")
                lines.append(f"- 适用: {', '.join(n.get('sector_scope', ['all']))}")
                excluded = n.get("sector_excluded", [])
                if excluded:
                    lines.append(f"- 排除: {', '.join(excluded)}")
                lines.append(f"- 置信: {n.get('confidence', 0.5)}")
                lines.append(f"- 检索文本: {n.get('retrieval_text', n.get('situation', ''))}")
                lines.append("")

        content = "\n".join(lines) + "\n"
        self.index_path.write_text(content, encoding="utf-8")
        return content

    def read_index(self) -> str:
        if not self.index_path.exists():
            self.rebuild_index()
        return self.index_path.read_text(encoding="utf-8")

    # ── 行业门限硬过滤 (PDF 2 §2.2.4) ──

    @staticmethod
    def sector_match(note: dict, sector: Optional[str]) -> bool:
        """笔记是否适用于当前股票行业。

        - sector 为 None / 'all' / 空 → 不过滤，全部通过
        - sector_scope = ['all'] → 通过
        - sector 在 sector_excluded → 拒绝
        - sector 在 sector_scope → 通过
        - 其他情况（scope 非 all 且 sector 不在 scope）→ 拒绝
        """
        if not sector or sector == "all":
            return True
        scope = note.get("sector_scope", ["all"])
        excluded = note.get("sector_excluded", [])
        if not isinstance(scope, list):
            scope = [scope]
        if not isinstance(excluded, list):
            excluded = [excluded]

        if sector in excluded:
            return False
        if "all" in scope:
            return True
        return sector in scope

    def list_notes_for_sector(self, sector: Optional[str],
                              include_archived: bool = False) -> list[dict]:
        """列出适用于指定行业的笔记（应用 sector_scope / sector_excluded 硬过滤）。"""
        all_notes = self.list_notes(include_archived=include_archived)
        if not sector or sector == "all":
            return all_notes
        return [n for n in all_notes if self.sector_match(n, sector)]

    def build_index_for_sector(self, sector: Optional[str]) -> str:
        """为指定行业构建一个临时索引文本（不写盘）。

        Recall 时传给 LLM 的索引在硬过滤后，候选池更准、token 更省。
        """
        notes = self.list_notes_for_sector(sector)
        if not notes:
            return f"# 情境记忆索引（行业 {sector} 适用）\n\n（暂无笔记）\n"

        lines = [
            f"# 情境记忆索引（行业 {sector} 适用）",
            f"> 已按 sector_scope / sector_excluded 硬过滤，共 {len(notes)} 条候选。",
            "",
            "## 召回检索区",
            "",
        ]
        for n in notes:
            lines.append(f"### `{n['id']}`")
            lines.append(f"- 情境: {n.get('situation', '')}")
            lines.append(f"- 适用: {', '.join(n.get('sector_scope', ['all']))}")
            lines.append(f"- 置信: {n.get('confidence', 0.5)}")
            lines.append(f"- 检索文本: {n.get('retrieval_text', n.get('situation', ''))}")
            lines.append("")
        return "\n".join(lines)

    # ── 辅助 ──

    def get_full_notes(self, note_ids: list[str]) -> list[dict]:
        """批量加载多条笔记完整内容（含 body）。"""
        result = []
        for nid in note_ids:
            n = self.load(nid)
            if n:
                result.append(n)
        return result
