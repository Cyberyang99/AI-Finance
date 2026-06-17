"""REPL 主循环 — fa chat.

设计：
  - Anthropic tool use 协议；多轮对话累积 messages
  - 结构化状态 state（最近 ticker/sector）+ 记忆概览 注入 system prompt
  - UI：rich 渲染（assistant 走 Markdown，工具调用/结果走淡色），不可用时回退纯 print
  - 输入：readline 历史（↑↓ 调历史、行内编辑，持久化到 ~/.fa_chat_history）
  - 上下文：超阈值按整轮裁剪旧消息，防 token 爆
  - 会话：自动落盘 memory/.chat_sessions/（gitignore），/load 可续聊
"""

from __future__ import annotations
import json
import os
import re
import select
import sys
from datetime import datetime
from pathlib import Path

from ..config import make_anthropic_client, load_config
from ..memory.store import PROJECT_DIR
from .tools import TOOLS_SPEC, dispatch

# ── rich UI（可用则用，不可用回退纯 print）──
try:
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.text import Text
    _console = Console()
    _HAS_RICH = True
except Exception:
    _console = None
    _HAS_RICH = False

# ── readline 输入历史（标准库，免依赖）──
try:
    import readline
    _HISTORY_FILE = os.path.expanduser("~/.fa_chat_history")
    try:
        readline.read_history_file(_HISTORY_FILE)
    except (FileNotFoundError, OSError):
        pass
    readline.set_history_length(2000)
    _HAS_READLINE = True
except Exception:
    _HAS_READLINE = False

SESSION_DIR = PROJECT_DIR / "memory" / ".chat_sessions"

# 修改类工具：跑完后刷新记忆概览
_MUTATING_TOOLS = {"ingest_doc", "import_files", "add_note", "delete_cot",
                   "delete_note", "merge_cot", "regroup_cot", "reclassify_cot",
                   "edit_cot_chain"}

# 工具原始返回默认折叠：内容随后由 assistant 复述，避免终端被 CoT 全文刷屏。
# 用 /verbose on 可强制展开全部。
_QUIET_TOOLS = {"find_ticker", "list_cot", "search_memory", "get_cot", "query_knowledge"}

# 上下文裁剪阈值
_MAX_CTX_CHARS = 60000
_KEEP_LAST_TURNS = 6


SYSTEM_PROMPT_TEMPLATE = """你是 fa 的对话助手 —— 帮用户用自然语言操作基本面研究 agent。

你能做的事：录入笔记、投喂研报、查询/搜索 CoT 与笔记、读全文回答问题、合并/重组/改分/软删除 CoT、查 ticker、看仪表盘等，全部通过工具调用完成。

## 🔴 硬规则（违反就是大错）

1. **永远不要给用户输出 shell 命令让他自己跑**。你的工作就是直接调用工具。绝对禁止生成 ```bash fa ...``` 让用户去终端执行。工具失败就老实说失败，不要 fallback 到贴命令。
2. **用户给文件路径 + 描述 = 投喂意图**：消息里出现文件路径（.pdf/.pptx/.docx/.xlsx）+ 内容描述时，默认用 ingest_doc 投喂，把描述当 comment。不要先 dry_run 再问，直接干。
3. **从描述里自动推断 sector**：用户说"核心看点在 AI-数据中心-电力-燃气轮机"就是 sector，直接用，不要再问。
4. **不要拆成"先预览再确认"二段式**：用户说"导入"就是要真跑。dry_run 只在用户明确说"预览/先看看"时用。

## 问答规则（重要）

5. **回答关于内容的问题前，先取全文再答**。用户问"X 的核心逻辑/推理链是什么""那份研报讲了啥"时：先 get_cot 拿全文，再据此回答，**不要凭空编**。用户问"我对 X 的看法"用 get_note。
6. **万能查库优先用 query_knowledge**：用户问"有没有关于 Y 的内容""把 X 相关 CoT/note 全找出来""全量显示/导出 Word/保存桌面"时，直接调 query_knowledge。需要 Word/桌面/导出时传 export_docx=true。命中很多时先简洁总结，再给导出路径。
7. **查某主题的 CoT → 一步到位用 list_cot 或 query_knowledge**：tag 用模糊词即可（"AI大模型"/"大模型" 会自动解析到规范主题，空格/拼写不敏感），**不要先查全称、不要反复换写法试**。看分数最低的传 `sort=asc`、最高传 `sort=desc`；只有用户明确要求"完整推理/全文"才传 `full=true`，否则保持简短列表。**用户只说主题时只传 tag，不要传 sector**（否则会被板块吞掉结果）。若返回"没有主题匹配"，照它给的现有主题名改一次即可，别空转。
8. **搜索/定位用对工具**：query_knowledge 做全量查库与导出；search_memory 做轻量关键词召回（多词空格分隔，全部命中）→ 命中带 id；看某一份文件全文用 get_cot（传 id 或标题片段）；看笔记全文用 get_note。
9. **改正/纠错用 edit_cot_chain**：用户要改某条链的主题/信号分/正文，或删掉某条没价值的链时，先从 list_cot/search_memory 拿到那条的 `id`（持久 uid，删别的链不会漂移），再调 `edit_cot_chain`。改前用一句话说清改的是哪条、改成什么；tag 必须是闭合词表里的主题。**删除是两段式**：先 `delete=true`（不带 confirm）拿到预览，把"即将删 信号X「trigger」"原样给用户、等用户明确说删，再带 `delete=true, confirm=true` 重发同一 cot_id 才真删（被删块归档到 _archive/ 可恢复）。**绝不在用户没看过预览前直接带 confirm=true。**

## 工作规则

10. **指代消解**：用户说"它""这只票""刚才那个"看「会话状态」的 last_ticker。
11. **公司名 → ticker**：用户提到中文公司名而你不确定 ticker 时先 find_ticker（用户描述里提了"豪迈科技"你就自己识别并解析，别让用户重说）。
12. **多工具串联自动跑**：一句话需要多步时按顺序连续调工具，不要中途问确认。
13. **同一个工具连续两次返回空/失败就停手**：换一种工具或如实告诉用户"没找到"，并说明已试过什么，**不要反复换关键词硬试**（这会撞工具循环上限）。
14. **软删除是安全的**：delete_cot/delete_note 只是归档到 _archive/（可恢复，绝不物理删）。删前简短说一句删的是哪份即可，不用反复确认。
15. **回话简洁**：工具跑完总结 1-2 行；工具自己流式打印了进度就只需简短确认。
16. **拒绝危险**：要求物理删数据库 / 改 .env / git push 时礼貌拒绝，不绕过。
17. **默认中文输出**：只有专有名词（公司简称、产品名、技术术语、ticker）才保留英文/原文，其余一律中文。提到标的优先用简称（如"豪迈科技"），不要甩 ticker 代码。
18. **主题优先**：CoT 记忆的主轴是「主题(tags)」，一级行业分类只是兜底补充。描述/归纳 CoT 时优先按主题组织，行业分类次之。

## 记忆概览（当前库里有什么）

{mem_overview}

## 当前会话状态

{state_block}
"""


# ──────────────── 渲染 ────────────────

def _say(text: str, style: str = ""):
    if _HAS_RICH:
        _console.print(text, style=style)
    else:
        print(text)


def _say_assistant(text: str):
    """assistant 文本走 markdown 渲染。"""
    if not text.strip():
        return
    if _HAS_RICH:
        _console.print()
        try:
            _console.print(Markdown(text))
        except Exception:
            _console.print(text)
    else:
        print(f"\n{text}")


def _say_tool_call(name: str, inp: dict):
    inp_str = json.dumps(inp, ensure_ascii=False)
    if len(inp_str) > 200:
        inp_str = inp_str[:200] + "..."
    if _HAS_RICH:
        _console.print(f"  🔧 [cyan]{name}[/cyan][dim]({inp_str})[/dim]")
    else:
        print(f"\n  🔧 调用 {name}({inp_str})")


def _is_error_result(result: str) -> bool:
    head = result.lstrip()[:8]
    return head.startswith(("✗", "错误", "工具执行异常", "⚠"))


def _say_tool_result(name: str, result: str, max_lines: int = 30, quiet: bool = False):
    # 只读工具且非错误：折叠成一行，内容由随后的 assistant 回复呈现，避免过程刷屏
    if quiet and not _is_error_result(result):
        n = len(result.split("\n"))
        msg = f"  [dim]← {name} ✓（{n} 行，/verbose 看原始返回）[/dim]" if _HAS_RICH else f"  ← {name} ✓（{n} 行）"
        _say(msg) if not _HAS_RICH else _console.print(msg)
        return
    lines = result.split("\n")
    shown = lines[:max_lines]
    body = "\n".join(f"    {ln}" for ln in shown)
    if len(lines) > max_lines:
        body += f"\n    [dim]... ({len(lines)} 行，省略 {len(lines)-max_lines})[/dim]" if _HAS_RICH else f"\n    ... ({len(lines)} 行，省略)"
    if _HAS_RICH:
        _console.print(f"  [dim]← {name}[/dim]")
        _console.print(body, style="dim", highlight=False)
    else:
        print(f"  ← {name} 返回：")
        print(body)


def _banner(model: str, mem_overview: str, resumed: str = ""):
    head = (f"fa chat — 自然语言对话模式   model={model}\n"
            f"/help 用法 · /quit 退出（或 Ctrl-D）"
            + (f"\n{resumed}" if resumed else ""))
    if _HAS_RICH:
        _console.print(Panel(head, title="fa", border_style="cyan"))
        _console.print(Panel(mem_overview, title="📚 记忆概览", border_style="dim"))
    else:
        print("=" * 60)
        print("  " + head.replace("\n", "\n  "))
        print("-" * 60)
        print(mem_overview)
        print("=" * 60)


# ──────────────── 记忆概览 ────────────────

def _ticker_label(ticker: str) -> str:
    """标的显示用简称（优先），查不到再退回代码。"""
    try:
        from .resolver import name_for_ticker
        return name_for_ticker(ticker) or ticker
    except Exception:
        return ticker


def _memory_overview() -> str:
    """汇总库里有什么，注入 system prompt 让 LLM 接地。

    主题(tag) 是 CoT 记忆的主轴，放最前；一级行业分类(sector) 作兜底补充。
    标的用简称展示，不用代码。
    """
    try:
        from collections import Counter
        from ..cot.merger import list_sectors_with_cots
        from ..cot import load_cots
        from ..ingest.user_note import load_user_notes
        cots = load_cots()

        # 主题分布（主轴）
        tag_counts: Counter = Counter()
        untagged = 0
        for c in cots:
            tags = c.get("_tags") or []
            if tags:
                for t in tags:
                    tag_counts[t] += 1
            else:
                untagged += 1
        tag_str = "、".join(f"{t}({n})" for t, n in tag_counts.most_common(14)) or "(暂无主题)"

        # 一级行业（兜底）
        sectors = list_sectors_with_cots()
        sec_str = "、".join(f"{s}({n})" for s, n in sectors[:10]) or "(无)"

        notes = load_user_notes()
        note_tickers = sorted({n["ticker"] for n in notes})
        labels = [_ticker_label(t) for t in note_tickers]

        parts = [
            f"- CoT 共 {len(cots)} 条。",
            f"  · 主题（主）：{tag_str}" + (f"；另有 {untagged} 条未打主题" if untagged else ""),
            f"  · 一级行业（兜底）：{sec_str}",
            f"- 用户笔记 {len(notes)} 条，覆盖 {len(note_tickers)} 个标的"
            + (f"：{'、'.join(labels[:18])}" if labels else ""),
        ]
        return "\n".join(parts)
    except Exception as e:
        return f"- (概览加载失败: {e})"


# ──────────────── 上下文 / 会话持久化 ────────────────

def _render_state(state: dict) -> str:
    parts = []
    for k in ("last_ticker", "last_ticker_candidates", "last_sector", "last_tags"):
        if state.get(k):
            parts.append(f"- {k}: {state[k]}")
    return "\n".join(parts) if parts else "- (空)"


def _blocks_to_dicts(content) -> list[dict]:
    """把 anthropic 返回的 content block 转成可序列化 dict（便于裁剪/落盘/回传）。

    必须保留所有 block 类型——尤其 thinking/redacted_thinking：思考模式下
    这些块必须原样回传给 API，否则报 400 (content[].thinking must be passed back)。
    """
    out = []
    for b in content:
        # 优先用 pydantic model_dump 拿到完整字段（含 thinking/signature）
        if hasattr(b, "model_dump"):
            try:
                out.append(b.model_dump(exclude_none=True))
                continue
            except Exception:
                pass
        t = getattr(b, "type", None)
        if t == "text":
            out.append({"type": "text", "text": b.text})
        elif t == "tool_use":
            out.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        elif t == "thinking":
            out.append({"type": "thinking", "thinking": getattr(b, "thinking", ""),
                        "signature": getattr(b, "signature", "")})
        elif t == "redacted_thinking":
            out.append({"type": "redacted_thinking", "data": getattr(b, "data", "")})
    return out


def _ctx_size(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)


def _trim_messages(messages: list[dict]) -> int:
    """超阈值时按整轮（user 文本消息为边界）从最旧处裁剪，保 tool_use/result 配对完整。

    返回裁掉的消息条数。
    """
    dropped = 0
    while _ctx_size(messages) > _MAX_CTX_CHARS:
        starts = [i for i, m in enumerate(messages)
                  if m["role"] == "user" and isinstance(m.get("content"), str)]
        if len(starts) <= _KEEP_LAST_TURNS:
            break
        cut = starts[1]  # 删掉第一整轮 [starts[0], starts[1])
        del messages[:cut]
        dropped += cut
    return dropped


def _save_session(messages: list[dict], session_file: Path, model: str):
    try:
        SESSION_DIR.mkdir(parents=True, exist_ok=True)
        payload = {"updated": datetime.now().isoformat(timespec="seconds"),
                   "model": model, "messages": messages}
        session_file.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    except Exception:
        pass  # 落盘失败不影响主流程


def _list_sessions() -> list[Path]:
    if not SESSION_DIR.exists():
        return []
    return sorted(SESSION_DIR.glob("session_*.json"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def _session_preview(p: Path) -> str:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        msgs = data.get("messages", [])
        first_user = next((m["content"] for m in msgs
                           if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        n_turns = sum(1 for m in msgs if m["role"] == "user" and isinstance(m.get("content"), str))
        return f"{data.get('updated', '?')}  {n_turns} 轮  「{str(first_user)[:30]}」"
    except Exception:
        return "(无法预览)"


HELP_TEXT = """\
用法示例：
  茅台代码多少
  搜一下提到燃气轮机的 CoT          → search_memory
  把燃气轮机相关 CoT/note 全文导出 Word → query_knowledge
  豪迈的核心逻辑是什么              → get_cot 取全文后回答
  我对宁德时代的看法是什么          → get_note
  合并 SoftwareServices 板块的 CoT  → merge_cot
  删掉曦智那份 CoT                  → delete_cot（软删除，可恢复）
  /tmp/茅台研报.pdf 给 600519 写笔记，重点产能稀缺

快捷入口（引导式，不靠 LLM 猜意图）：
  1 / 2 / 3 / 4  投喂研报→CoT / 录入 note / vet 三模式 / 知识库查询+Word导出
  5             维护工具箱（未分类、主题、清理）
  m             调出快捷菜单
  /cot <路径>   直接上传研报提炼 CoT
  /vet <代码> [想法]  直接校验个股逻辑
  /consolidate [代码]  手动生成 company synthesis；不带代码则跑首页维护

特殊命令：
  /reset        清空对话历史和状态
  /state        查看会话状态
  /mem          刷新并查看记忆概览
  /confirm on   每次工具调用前 y/n 确认；/confirm off 关闭（默认关）
  /verbose on   展示所有工具的原始返回；/verbose off 折叠检索/导航类（默认关）
  /multi        多行输入模式，空行提交
  /save         手动保存当前会话
  /sessions     列出历史会话
  /load [n]     载入第 n 个历史会话续聊（不带 n 列出列表）
  /quit         退出（或 Ctrl-D）

输入历史：↑↓ 调最近输入（持久化到 ~/.fa_chat_history）；直接粘贴多行会自动合并
Ctrl-C：输入时取消当前行；工具执行中中断该工具，保留会话
"""


def _confirm_tool_call(name: str, inp: dict) -> str:
    while True:
        try:
            ans = input(f"  ▶ 执行 {name}? [Y/n/q=取消整轮] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  → 已取消")
            return "q"
        if ans in ("", "y", "yes"):
            return "y"
        if ans in ("n", "no"):
            return "n"
        if ans in ("q", "quit"):
            return "q"
        print("  请输入 y / n / q")


def _entry_status_line() -> str:
    try:
        from ..cot import load_cots
        from ..ingest.user_note import load_user_notes
        cots = load_cots()
        notes = load_user_notes()
        untagged = sum(1 for c in cots if not (c.get("_tags") or []))
        return f"库状态: CoT {len(cots)} 条 | 未打主题 {untagged} 条 | note {len(notes)} 条"
    except Exception as e:
        return f"库状态: 加载失败 ({e})"


def _quick_menu_text() -> str:
    return f"""\
┌─ fa chat 工作台 ─────────────────────────────────────
│ {_entry_status_line()}
│
│ [1] 投喂研报 -> 提炼 CoT
│ [2] 录入 / 上传 note
│ [3] vet 校验 -> 个股 / 想法 / 批量
│ [4] 知识库查询 -> 一句话搜 CoT + note / Word 导出
│
│ [5] 维护工具箱（低频：未分类、主题、清理）
│ 直接说话 = 自由对话 · m = 菜单 · /help = 全部命令 · /quit = 退出
└──────────────────────────────────────────────────────"""


def _company_synthesis_homepage() -> dict:
    """fa chat 首页维护：自动综合最近新增/更新且 note>=2 的个股。"""
    try:
        from ..consolidate import stale_synthesis_candidates, auto_consolidate_stale
    except Exception as e:
        return {"error": f"加载 consolidate 失败: {e}"}

    try:
        limit = int(os.environ.get("FA_CHAT_AUTO_CONSOLIDATE_LIMIT", "3"))
    except ValueError:
        limit = 3
    try:
        recent_days = int(os.environ.get("FA_CHAT_AUTO_CONSOLIDATE_DAYS", "30"))
    except ValueError:
        recent_days = 30
    if os.environ.get("FA_CHAT_AUTO_CONSOLIDATE", "1").lower() in ("0", "false", "no"):
        return {"skipped": "FA_CHAT_AUTO_CONSOLIDATE=0"}

    candidates = stale_synthesis_candidates(min_notes=2, recent_days=recent_days, limit=limit)
    if not candidates:
        return {"candidates": [], "results": [], "recent_days": recent_days, "limit": limit}

    print(f"\n[首页维护] 发现 {len(candidates)} 个需要更新 company synthesis 的标的，开始自动综合...")
    for c in candidates:
        print(f"  - {c['ticker']}：{c['note_count']} 份 note")
    return auto_consolidate_stale(min_notes=2, recent_days=recent_days, limit=limit)


def _render_synthesis_homepage(result: dict) -> str:
    if result.get("error"):
        return f"company synthesis 首页维护失败：{result['error']}"
    if result.get("skipped"):
        return f"company synthesis 首页维护已跳过：{result['skipped']}"
    candidates = result.get("candidates") or []
    if not candidates:
        return "company synthesis：没有需要更新的标的（条件：最近新增/更新 note，且 note>=2）。"
    lines = ["company synthesis 首页维护："]
    for r in result.get("results", []):
        if r.get("ok"):
            lines.append(f"- {r['ticker']}：已更新 {Path(r['path']).name}（notes={r['note_count']}）")
        else:
            lines.append(f"- {r['ticker']}：失败（{r.get('error', 'unknown')}）")
    return "\n".join(lines)


def _ask(prompt: str):
    """引导式单行输入；Ctrl-C / Ctrl-D / 空 q 视为取消，返回 None。"""
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  → 已取消")
        return None
    return None if v.lower() in ("q", "quit", "取消") else v


def _stdin_ready(timeout: float = 0.03) -> bool:
    """Best-effort 检测粘贴多行时 input() 后面已经排队的行。"""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
        return bool(ready)
    except Exception:
        return False


def _read_multiline_until_blank(prompt: str = "fa| "):
    """显式多行输入：空行提交。"""
    lines = []
    while True:
        try:
            line = input(prompt if not lines else "... ")
        except (EOFError, KeyboardInterrupt):
            raise
        if not line:
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _read_user_input() -> str:
    """读取用户输入。

    - 常规单行：回车即提交。
    - 直接粘贴多行：读取 first line 后，把 stdin 缓冲区里已排队的后续行一并收进来。
    - `/multi`：显式进入多行模式，空行提交。
    """
    first = input("\nfa> ")
    if first.strip() == "/multi":
        return _read_multiline_until_blank()

    lines = [first]
    # 处理多行粘贴：后续行通常已经在 stdin 队列里；短暂轮询，不等待用户手打。
    while _stdin_ready():
        nxt = sys.stdin.readline()
        if nxt == "":
            break
        lines.append(nxt.rstrip("\n"))
    return "\n".join(lines).strip()


def _quick_action(choice: str, state: dict) -> None:
    """数字快捷 → 引导式确定性流程（不经 LLM 路由，消除上传歧义）。"""
    from .tools import _do_ingest_doc, _do_add_note, _do_list_notes

    if choice == "1":
        print("  [上传研报 → 提炼 CoT]")
        path = _ask("  文件路径 (pdf/pptx/docx/xlsx/txt/md): ")
        if not path:
            return
        comment = _ask("  一句话角度 / 重点 (可空): ") or ""
        # 不再追问重抽：新文件直接提炼；已提炼过的会返回提示，需重抽用 /cot 或 CLI --force
        print(_do_ingest_doc({"file_path": path, "comment": comment}, state))

    elif choice == "2":
        print("  [上传 / 录入 个股 note]")
        ticker = _ask("  股票代码或公司名: ")
        if not ticker:
            return
        path = _ask("  文件路径 (可空；留空则手动写想法): ") or ""
        if path:
            comment = _ask("  一句话评论 / 角度 (可空): ") or ""
            print(_do_add_note({"ticker": ticker, "file_path": path, "comment": comment}, state))
        else:
            msg = _ask("  你的论点 / 想法: ")
            if not msg:
                print("  → 空，已取消")
                return
            print(_do_add_note({"ticker": ticker, "message": msg}, state))

    elif choice == "3":
        print("  [vet 校验]")
        mode = _ask("  模式 [1] 个股  [2] 纯想法  [3] 批量清单  (回车=1): ") or "1"
        from ..vet import vet_stock, vet_idea, parse_batch_input, vet_batch
        if mode == "1":
            ticker = _ask("  股票代码 / 公司名: ")
            if not ticker:
                return
            idea = _ask("  你的想法 (可空；输入 /multi 可多行): ") or ""
            if idea.strip() == "/multi":
                idea = _read_multiline_until_blank("idea| ")
            res = vet_stock(ticker, idea=idea)
            if res.get("error"):
                print(f"  ✗ {res['error']}")
            else:
                _say_assistant(res["markdown"])
                if res.get("path"):
                    print(f"\n  ✓ 已落盘: {res['path']}（未入库，满意可自行收录）")
        elif mode == "2":
            idea = _ask("  输入观点 / 文件路径 (输入 /multi 可多行): ")
            if not idea:
                return
            if idea.strip() == "/multi":
                idea = _read_multiline_until_blank("idea| ")
            tag = _ask("  主题过滤 (可空，如 AI算力/机器人): ") or ""
            res = vet_idea(idea, tag=tag)
            if res.get("error"):
                print(f"  ✗ {res['error']}")
            else:
                _say_assistant(res["markdown"])
                if res.get("path"):
                    print(f"\n  ✓ 已落盘: {res['path']}")
        elif mode == "3":
            batch = _ask("  清单路径(xlsx/csv/txt) 或 ticker 列表: ")
            if not batch:
                return
            top_raw = _ask("  每股命中 CoT 数 (默认 5): ") or "5"
            try:
                top = max(1, min(int(top_raw), 20))
            except ValueError:
                top = 5
            items, warns = parse_batch_input(batch)
            for w in warns:
                print(f"  [batch] {w}")
            if not items:
                print("  ✗ 无有效标的")
                return
            res = vet_batch(items, top=top, save=True)
            print(f"  ✓ 批量完成 → {res.get('path')}")
            for r in res.get("results", [])[:8]:
                score = r.get("fit_score")
                print(f"    {r.get('ticker')}  契合度={score if score is not None else '?'}  {r.get('verdict', '')[:60]}")
            if len(res.get("results", [])) > 8:
                print(f"    ... 还有 {len(res['results']) - 8} 只，详见 Excel")
        else:
            print("  → 无效模式，已取消")

    elif choice == "4":
        print("  [知识库查询：CoT + note]")
        from .knowledge import query_knowledge
        query = _ask("  想查什么？可写关键词 / 公司 / ticker / 主题（输入 /multi 可多行）: ") or ""
        if query.strip() == "/multi":
            query = _read_multiline_until_blank("query| ")
        display_n = 10
        res = query_knowledge(
            query=query, scope="all", max_items=display_n, full=True,
        )
        print(res["text"])
        total = int(res.get("total") or 0)
        if total > display_n:
            ans = _ask(f"  命中 {total} 条，是否导出 Word 到桌面？[Y/n] ") or "y"
            if ans.lower() not in ("n", "no", "否"):
                export_raw = _ask("  Word 最多收录多少条 (默认 80，最高 200): ") or "80"
                try:
                    export_n = max(1, min(int(export_raw), 200))
                except ValueError:
                    export_n = 80
                exported = query_knowledge(
                    query=query, scope="all", max_items=export_n, full=True,
                    export_docx=True,
                )
                print(f"  ✓ Word 已导出: {exported.get('docx_path')}")

    elif choice == "5":
        print("  [维护工具箱]")
        print(_memory_overview())
        print("  可直接说：查看未分类 CoT / 把某份 CoT 改主题 / 合并某板块 CoT / 重打分某份 CoT")


# ── 上传意图询问（问题3：路径+无明确意图 → 先问 CoT/note/both，减少误操作）──

_DOC_EXT = r"(?:pdf|pptx|ppt|docx|doc|xlsx|xls|md|txt)"
_COT_INTENT = ("cot", "思维链", "逻辑链", "提炼", "提链", "提取链")
_NOTE_INTENT = ("笔记", "note", "论点", "看法", "观点")
_BOTH_INTENT = ("都要", "都提", "两个都", "both", "二者都", "都做", "全都要")


def _extract_file_path(text: str) -> str | None:
    """从自由文本里抓一个文档路径。先认引号包裹（支持空格），再认无空格 token。"""
    m = re.search(r'["\'](.+?\.' + _DOC_EXT + r')["\']', text, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'(\S+\.' + _DOC_EXT + r')', text, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _has_upload_intent_word(text: str) -> bool:
    t = text.lower()
    return any(w in t for w in _COT_INTENT + _NOTE_INTENT + _BOTH_INTENT)


def _handle_upload_intent(user_input: str, state: dict) -> bool:
    """命中"给了文件路径但没说要 CoT/note"时，弹三选一再分派；返回 True 表示已处理。

    意图明确（含 CoT/note/both 关键词）或没有文件路径 → 返回 False，交回 LLM 主循环。
    """
    path = _extract_file_path(user_input)
    if not path or _has_upload_intent_word(user_input):
        return False

    from .tools import _do_ingest_doc, _do_add_note
    # 把路径从原文里抠掉，剩下的当作角度/评论传给下游
    comment = re.sub(r'["\']?' + re.escape(path) + r'["\']?', "", user_input).strip()

    _say(f"  检测到文件：{path}", style="cyan")
    choice = _ask("  这份要做什么？[1] 提炼 CoT  [2] 录入 note  [3] 两者都要  (回车=1，q 取消) ")
    if choice is None:
        print("  → 已取消")
        return True
    choice = choice or "1"
    if choice not in ("1", "2", "3"):
        print("  → 无效选择，已取消（可重新发送，或直接说『提 CoT』/『写笔记』跳过此问）")
        return True

    if choice in ("1", "3"):
        print(_do_ingest_doc({"file_path": path, "comment": comment}, state))
    if choice in ("2", "3"):
        ticker = _ask("  录入 note 需要标的，请给股票代码或公司名: ")
        if ticker:
            print(_do_add_note({"ticker": ticker, "file_path": path, "comment": comment}, state))
        else:
            print("  → 未给标的，note 部分已跳过")
    return True


def run_repl(model: str | None = None, max_iterations: int | None = None):
    """启动 chat REPL。max_iterations: 单轮工具调用最大轮数（防死循环）。
    缺省读环境变量 FA_CHAT_MAX_ITER（默认 30）。"""
    if max_iterations is None:
        try:
            max_iterations = int(os.environ.get("FA_CHAT_MAX_ITER", "30"))
        except ValueError:
            max_iterations = 30
    cfg = load_config().get("agent", {})
    model = model or cfg.get("model", "deepseek-v4-flash")

    try:
        client = make_anthropic_client()
    except Exception as e:
        print(f"[CHAT] 初始化 LLM 客户端失败: {e}")
        return

    state: dict = {"confirm_mode": False}
    messages: list[dict] = []
    mem_overview = _memory_overview()
    interrupt_count = 0
    session_file = SESSION_DIR / f"session_{datetime.now():%Y%m%d-%H%M%S}.json"

    _banner(model, mem_overview)
    synth_home = _company_synthesis_homepage()
    print("\n" + _render_synthesis_homepage(synth_home))
    if synth_home.get("results"):
        mem_overview = _memory_overview()
    print("\n" + _quick_menu_text())

    while True:
        try:
            user_input = _read_user_input()
            interrupt_count = 0
            if _HAS_READLINE:
                try:
                    readline.write_history_file(_HISTORY_FILE)
                except OSError:
                    pass
        except EOFError:
            _save_session(messages, session_file, model)
            print("\n再见 👋")
            return
        except KeyboardInterrupt:
            interrupt_count += 1
            if interrupt_count >= 2:
                print("\n再见 👋")
                return
            print("\n  (Ctrl-C 再按一次退出，或输入 /quit)")
            continue

        if not user_input:
            continue

        # ── 斜杠命令 ──
        if user_input in ("/quit", "/exit", "/q"):
            _save_session(messages, session_file, model)
            print("再见 👋")
            return
        if user_input in ("/help", "/?", "?"):
            print(HELP_TEXT)
            continue
        if user_input == "/reset":
            messages.clear()
            state.clear()
            state["confirm_mode"] = False
            mem_overview = _memory_overview()
            print("[已清空对话历史和状态]")
            continue
        if user_input == "/state":
            print(json.dumps(state, ensure_ascii=False, indent=2))
            continue
        if user_input == "/mem":
            mem_overview = _memory_overview()
            _say(mem_overview, style="dim")
            continue
        if user_input == "/save":
            _save_session(messages, session_file, model)
            print(f"[已保存 → {session_file.name}]")
            continue
        if user_input == "/sessions":
            sess = _list_sessions()
            if not sess:
                print("[无历史会话]")
            else:
                print(f"=== 历史会话 ({len(sess)}) ===")
                for i, p in enumerate(sess[:20], 1):
                    print(f"  {i}. {_session_preview(p)}")
                print("用 /load n 载入续聊")
            continue
        if user_input.startswith("/load"):
            sess = _list_sessions()
            parts = user_input.split()
            if len(parts) < 2:
                print(f"=== 历史会话 ({len(sess)}) ===")
                for i, p in enumerate(sess[:20], 1):
                    print(f"  {i}. {_session_preview(p)}")
                print("用 /load n 载入第 n 个")
                continue
            try:
                idx = int(parts[1]) - 1
                data = json.loads(sess[idx].read_text(encoding="utf-8"))
                messages = data.get("messages", [])
                session_file = sess[idx]
                print(f"[已载入 {sess[idx].name}，{len(messages)} 条消息，继续聊]")
            except (ValueError, IndexError):
                print("[无效编号，用 /sessions 看列表]")
            except Exception as e:
                print(f"[载入失败: {e}]")
            continue
        if user_input.startswith("/confirm"):
            parts = user_input.split()
            if len(parts) >= 2 and parts[1] in ("on", "off"):
                state["confirm_mode"] = parts[1] == "on"
                print(f"[confirm 模式: {'开 — 每次工具调用前确认' if state['confirm_mode'] else '关 — 直跑'}]")
            else:
                print(f"[当前 confirm: {'开' if state.get('confirm_mode') else '关'}]，用 /confirm on|off 切换")
            continue
        if user_input.startswith("/verbose"):
            parts = user_input.split()
            if len(parts) >= 2 and parts[1] in ("on", "off"):
                state["verbose_tools"] = parts[1] == "on"
                print(f"[verbose 模式: {'开 — 展示所有工具原始返回' if state['verbose_tools'] else '关 — 检索/导航类工具结果折叠成一行'}]")
            else:
                print(f"[当前 verbose: {'开' if state.get('verbose_tools') else '关'}]，用 /verbose on|off 切换")
            continue

        # ── 数字快捷 / 菜单（引导式确定性流程，避免 LLM 误判上传意图）──
        if user_input in ("m", "menu", "/menu", "0"):
            print(_quick_menu_text())
            continue
        if user_input in ("1", "2", "3", "4", "5", "/maintain"):
            if user_input == "/maintain":
                user_input = "5"
            _quick_action(user_input, state)
            continue
        # ── slash 快捷上传（熟练后直接用）──
        if user_input.startswith("/cot"):
            path = user_input[len("/cot"):].strip()
            if path:
                from .tools import _do_ingest_doc
                print(_do_ingest_doc({"file_path": path}, state))
            else:
                print("用法: /cot <文件路径>")
            continue
        if user_input.startswith("/vet"):
            rest = user_input[len("/vet"):].strip().split(maxsplit=1)
            if not rest:
                print("用法: /vet <股票代码> [想法]")
            else:
                from ..vet import vet_stock
                r = vet_stock(rest[0], idea=(rest[1] if len(rest) > 1 else ""))
                if r.get("error"):
                    print(f"  ✗ {r['error']}")
                else:
                    _say_assistant(r["markdown"])
                    if r.get("path"):
                        print(f"\n  ✓ 已落盘: {r['path']}（未入库）")
            continue
        if user_input.startswith("/consolidate"):
            rest = user_input[len("/consolidate"):].strip()
            if rest:
                from ..consolidate import build_company_synthesis
                r = build_company_synthesis(rest, save=True)
                if r.get("error"):
                    print(f"  ✗ {r['error']}")
                else:
                    print(f"  ✓ 综合稿: {r['path']}")
                    print(f"  ✓ 冲突清单: {r['conflict_path']}")
            else:
                r = _company_synthesis_homepage()
                print(_render_synthesis_homepage(r))
                if r.get("results"):
                    mem_overview = _memory_overview()
            continue

        # 上传意图询问：给了文件路径但没说要 CoT/note → 先问再做（不静默默认）
        if _handle_upload_intent(user_input, state):
            continue

        messages.append({"role": "user", "content": user_input})
        dropped = _trim_messages(messages)
        if dropped:
            _say(f"  [上下文裁剪：移除最旧 {dropped} 条消息]", style="dim yellow")

        # ── tool use 循环 ──
        for iteration in range(max_iterations):
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
                mem_overview=mem_overview, state_block=_render_state(state))
            try:
                if _HAS_RICH:
                    with _console.status("[dim]思考中…[/dim]", spinner="dots"):
                        resp = client.messages.create(
                            model=model, max_tokens=2000, system=system_prompt,
                            tools=TOOLS_SPEC, messages=messages)
                else:
                    resp = client.messages.create(
                        model=model, max_tokens=2000, system=system_prompt,
                        tools=TOOLS_SPEC, messages=messages)
            except Exception as e:
                _say(f"  [LLM 错误] {e}", style="red")
                messages.pop()  # 弹回脏 user message
                break

            # assistant content 转 dict 存入历史（可序列化、可裁剪、可落盘）
            assistant_blocks = _blocks_to_dicts(resp.content)
            messages.append({"role": "assistant", "content": assistant_blocks})

            text_blocks = [b for b in resp.content if b.type == "text"]
            tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]

            for tb in text_blocks:
                _say_assistant(tb.text)

            if not tool_use_blocks:
                break

            tool_results = []
            user_aborted_round = False
            mutated = False
            for tu in tool_use_blocks:
                _say_tool_call(tu.name, tu.input)

                if state.get("confirm_mode"):
                    decision = _confirm_tool_call(tu.name, tu.input)
                    if decision == "q":
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                             "content": "用户取消了整轮（剩余工具未执行）"})
                        user_aborted_round = True
                        break
                    if decision == "n":
                        _say_tool_result(tu.name, "用户跳过了这个工具调用")
                        tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                             "content": "用户跳过了这个工具调用"})
                        continue

                try:
                    result = dispatch(tu.name, tu.input, state)
                    if tu.name in _MUTATING_TOOLS:
                        mutated = True
                except KeyboardInterrupt:
                    print("\n  ⛔ 用户中断了工具执行")
                    result = "用户按 Ctrl-C 中断了这个工具，保留会话状态。"
                except Exception as e:
                    result = f"工具执行异常: {e}"
                quiet = (
                    not state.get("verbose_tools")
                    and tu.name in _QUIET_TOOLS
                )
                _say_tool_result(tu.name, result, quiet=quiet)
                tool_results.append({"type": "tool_result", "tool_use_id": tu.id,
                                     "content": result})

            messages.append({"role": "user", "content": tool_results})

            if mutated:
                mem_overview = _memory_overview()  # 库变了，刷新概览

            if user_aborted_round or resp.stop_reason != "tool_use":
                break
        else:
            _say(f"  [警告] 达到工具循环上限 {max_iterations}，强制中断"
                 f"（可调高环境变量 FA_CHAT_MAX_ITER）", style="yellow")

        _save_session(messages, session_file, model)  # 每轮结束自动落盘
