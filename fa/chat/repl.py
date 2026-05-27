"""REPL 主循环 — fa chat.

设计：
  - Anthropic tool use 协议
  - 多轮对话：每轮把 user input / assistant text+tool_use / user tool_result 累积到 messages
  - 结构化状态 state（最近 ticker / sector）注入 system prompt，让 LLM 知道"它"指代什么
  - 工具执行前打印工具名+参数让用户看见
"""

from __future__ import annotations
import json
import sys
from typing import Any

from ..config import make_anthropic_client, load_config
from .tools import TOOLS_SPEC, dispatch


SYSTEM_PROMPT_TEMPLATE = """你是 fa 的对话助手 —— 帮用户用自然语言操作基本面研究 agent。

你能做的事：通过工具调用来完成笔记录入、文件投喂、查询 CoT、查 ticker、看仪表盘等。

## 🔴 硬规则（违反就是大错）

1. **永远不要给用户输出 shell 命令让他自己跑**。你的工作就是直接调用工具。绝对禁止生成
   ```bash fa import ... ```
   这种内容让用户去终端执行。如果工具失败/拒绝，你就老实说工具失败了，不要 fallback 到贴命令。

2. **用户给文件路径 + 描述 = 投喂意图**：当用户消息里出现一个文件路径（.pdf/.pptx/.docx/.xlsx）+ 一段对内容的描述时，**默认意图就是用 ingest_doc 投喂这个文件**，把用户的描述作为 comment 参数。不要先 dry_run 再问，直接干。

3. **从用户描述里自动推断 sector**：用户说"核心看点在 AI-数据中心-电力-燃气轮机"——这就是 sector，直接当 sector 参数用，不要再问。

4. **不要拆解成"先预览再确认"二段式**：用户说"导入"就是要真跑。dry_run 只在用户明确说"预览/先看看/扫一下"时才用。

## 工作规则

5. **指代消解**：用户说"它"、"这只票"、"刚才那个"时，看「会话状态」的 last_ticker。
6. **公司名 → ticker**：用户提到中文公司名而你不确定 ticker 时，先调 find_ticker（注意：用户在描述里就提了公司名，比如"豪迈科技"，你应该自己识别并 find_ticker，不要让用户再说一遍）。
7. **多工具串联自动跑**：用户一句话需要多步时，按顺序连续调用工具，**不要在中间问用户确认**。
8. **回话简洁**：工具跑完总结 1-2 行就够；如果工具自己已经流式打印了进度，你只需简短确认"完成"。
9. **拒绝危险**：用户要求删数据库 / 改 .env / git push 时，礼貌拒绝，不调工具绕过。
10. **保持中文**。

## 当前会话状态

{state_block}
"""


def _render_state(state: dict) -> str:
    parts = []
    if state.get("last_ticker"):
        parts.append(f"- last_ticker: {state['last_ticker']}")
    if state.get("last_ticker_candidates"):
        parts.append(f"- last_ticker_candidates: {state['last_ticker_candidates']}")
    if state.get("last_sector"):
        parts.append(f"- last_sector: {state['last_sector']}")
    return "\n".join(parts) if parts else "- (空)"


def _print_tool_call(name: str, inp: dict):
    """工具调用前打印给用户看。"""
    inp_str = json.dumps(inp, ensure_ascii=False)
    if len(inp_str) > 200:
        inp_str = inp_str[:200] + "..."
    print(f"\n  🔧 调用 {name}({inp_str})")


def _print_tool_result(name: str, result: str):
    """工具结果回显。截断超长的。"""
    print(f"  ← {name} 返回：")
    for line in result.split("\n")[:30]:
        print(f"    {line}")
    if result.count("\n") > 30:
        print(f"    ... ({result.count(chr(10))} 行，省略)")


HELP_TEXT = """\
用法示例：
  茅台代码多少
  帮我把桌面 ai 文件夹的研报预览一下，板块是 AI
  /tmp/茅台研报.pdf 这份资料给 600519 写笔记，重点是产能稀缺
  列一下我的所有笔记
  看一下 AI 板块的 CoT

特殊命令：
  /reset       清空对话历史和状态
  /state       查看当前会话状态
  /confirm on  开启逐步确认：每次工具调用前 y/n 确认
  /confirm off 关闭逐步确认（默认 yolo 模式）
  /quit        退出 (或按 Ctrl-D)

Ctrl-C 行为：
  - 输入提示符时按 Ctrl-C：取消当前输入，回到提示符（连按两次或 /quit 退出）
  - 工具执行中按 Ctrl-C：中断当前工具，保留会话继续聊
"""


def _confirm_tool_call(name: str, inp: dict) -> str:
    """逐步确认模式下，工具调用前问用户。返回 'y' 执行 / 'n' 跳过 / 'q' 取消整轮。"""
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


def run_repl(model: str | None = None, max_iterations: int = 6):
    """启动 chat REPL。

    max_iterations: 单轮用户输入触发的工具调用最大轮数（防死循环）。
    """
    cfg = load_config().get("agent", {})
    model = model or cfg.get("model", "deepseek-v4-flash")

    try:
        client = make_anthropic_client()
    except Exception as e:
        print(f"[CHAT] 初始化 LLM 客户端失败: {e}")
        return

    print("=" * 60)
    print("  fa chat — 自然语言对话模式")
    print(f"  model={model}")
    print("  输入 /help 查看用法，/quit 退出（或 Ctrl-D）")
    print("=" * 60)

    state: dict = {"confirm_mode": False}
    messages: list[dict] = []
    interrupt_count = 0  # 连按两次 Ctrl-C 才退出

    while True:
        try:
            user_input = input("\nfa> ").strip()
            interrupt_count = 0
        except EOFError:
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
        if user_input in ("/quit", "/exit", "/q"):
            print("再见 👋")
            return
        if user_input in ("/help", "/?", "?"):
            print(HELP_TEXT)
            continue
        if user_input == "/reset":
            messages.clear()
            state.clear()
            state["confirm_mode"] = False
            print("[已清空对话历史和状态]")
            continue
        if user_input == "/state":
            print(json.dumps(state, ensure_ascii=False, indent=2))
            continue
        if user_input.startswith("/confirm"):
            parts = user_input.split()
            if len(parts) >= 2 and parts[1] in ("on", "off"):
                state["confirm_mode"] = parts[1] == "on"
                print(f"[confirm 模式: {'开 — 每次工具调用前确认' if state['confirm_mode'] else '关 — yolo 直跑'}]")
            else:
                print(f"[当前 confirm 模式: {'开' if state.get('confirm_mode') else '关'}]，用 /confirm on|off 切换")
            continue

        messages.append({"role": "user", "content": user_input})

        # tool use 循环
        for iteration in range(max_iterations):
            system_prompt = SYSTEM_PROMPT_TEMPLATE.format(state_block=_render_state(state))
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=2000,
                    system=system_prompt,
                    tools=TOOLS_SPEC,
                    messages=messages,
                )
            except Exception as e:
                print(f"  [LLM 错误] {e}")
                # 把这条 user message 弹回去，避免脏对话
                messages.pop()
                break

            # 收集 assistant 的所有 block，原样放进对话历史
            messages.append({"role": "assistant", "content": resp.content})

            # 拿出文本和 tool_use
            text_blocks = [b for b in resp.content if b.type == "text"]
            tool_use_blocks = [b for b in resp.content if b.type == "tool_use"]

            for tb in text_blocks:
                if tb.text.strip():
                    print(f"\n{tb.text}")

            if not tool_use_blocks:
                # 没调工具，对话轮结束
                break

            # 执行工具，把结果攒成 user 角色的 tool_result 列表
            tool_results = []
            user_aborted_round = False
            for tu in tool_use_blocks:
                _print_tool_call(tu.name, tu.input)

                # 逐步确认模式
                if state.get("confirm_mode"):
                    decision = _confirm_tool_call(tu.name, tu.input)
                    if decision == "q":
                        result = "用户取消了整轮（剩余工具未执行）"
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result,
                        })
                        user_aborted_round = True
                        break
                    if decision == "n":
                        result = "用户跳过了这个工具调用"
                        _print_tool_result(tu.name, result)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": tu.id,
                            "content": result,
                        })
                        continue

                try:
                    result = dispatch(tu.name, tu.input, state)
                except KeyboardInterrupt:
                    print("\n  ⛔ 用户中断了工具执行")
                    result = "用户按 Ctrl-C 中断了这个工具，保留会话状态。"
                except Exception as e:
                    result = f"工具执行异常: {e}"
                _print_tool_result(tu.name, result)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })

            messages.append({"role": "user", "content": tool_results})

            if user_aborted_round or resp.stop_reason != "tool_use":
                break
        else:
            print(f"  [警告] 达到工具循环上限 {max_iterations}，强制中断")
