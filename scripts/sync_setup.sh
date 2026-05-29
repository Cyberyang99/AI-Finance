#!/usr/bin/env bash
# AI-Finance 双机同步 — macOS 版（对标 scripts/sync_setup.ps1 的 Windows 版）。
#
# 设计：
#   git 跟踪的东西（framework/、sectors.yaml、README）留在原地。
#   .gitignore 的私有 md 数据（knowledge/cot、theses/user、situations、
#     episodic、raw）软链到 OneDrive/AI-Finance-data/memory，两机共用。
#
#   ⚠️ 与 Windows 版的关键区别：agent.db 不软链。
#      决策：db 各机本地一份，避免 SQLite 文件经 OneDrive 双机同时写导致损坏。
#      代价：ingested_docs 台账 / theses / reviews 等 db 内容不跨机自动同步
#      （CoT/论点等 md 文件已软链共享，fa cot/dash/recall 全部读文件系统，功能不受影响）。
#
# 用法：
#   有数据的机器先 git clone 并把私有数据 push 到 OneDrive（手动或在另一机跑），然后本机：
#     bash scripts/sync_setup.sh link      # 把本地 md 目录替换为指向 OneDrive 的软链
#     bash scripts/sync_setup.sh status    # 只看状态
#     bash scripts/sync_setup.sh unlink    # 解除软链（从 OneDrive 拷回实体，安全回退）
set -euo pipefail

MODE="${1:-status}"
PROJECT_ROOT="${PROJECT_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
CLOUD_ROOT="${CLOUD_ROOT:-$HOME/Library/CloudStorage/OneDrive-个人/AI-Finance-data}"
MEM="$PROJECT_ROOT/memory"
CLOUD_MEM="$CLOUD_ROOT/memory"

# 要软链的 memory/ 子路径（不含 agent.db）
ITEMS=("knowledge/cot" "theses/user" "episodic" "situations" "raw")

c()  { printf '\033[36m[%s]\033[0m %s\n' "$MODE" "$1"; }
ok() { printf '\033[32m[%s]\033[0m %s\n' "$MODE" "$1"; }
sk() { printf '\033[90m[%s]\033[0m %s\n' "$MODE" "$1"; }

[ -d "$MEM" ] || { echo "memory/ 不存在: $MEM" >&2; exit 1; }

if [ "$MODE" = "status" ]; then
  c "project: $MEM"
  c "cloud:   $CLOUD_MEM"
  [ -d "$CLOUD_MEM" ] || sk "cloud 尚未初始化"
  for it in "${ITEMS[@]}"; do
    local_p="$MEM/$it"; cloud_p="$CLOUD_MEM/$it"
    if [ -L "$local_p" ]; then st="SYMLINK -> $(readlink "$local_p")"
    elif [ -e "$local_p" ]; then st="LOCAL(实体)"
    else st="MISSING"; fi
    cl=$([ -e "$cloud_p" ] && echo YES || echo NO)
    printf "  %-16s local:%-30s cloud:%s\n" "$it" "$st" "$cl"
  done
  exit 0
fi

if [ "$MODE" = "link" ]; then
  c "LINK: 把本地 md 目录替换为指向 OneDrive 的软链"
  [ -d "$CLOUD_MEM" ] || { echo "cloud $CLOUD_MEM 不存在，等 OneDrive 同步或先在源机准备数据" >&2; exit 1; }
  for it in "${ITEMS[@]}"; do
    local_p="$MEM/$it"; cloud_p="$CLOUD_MEM/$it"
    mkdir -p "$cloud_p"                      # 保证云端目录存在
    if [ -L "$local_p" ]; then sk "$it 已是软链，跳过"; continue; fi
    if [ -e "$local_p" ]; then
      # 本地有实体：合并步骤应已把独有内容并入云端，这里仅备份后替换
      bak="$local_p.bak.local.$(date +%Y%m%d-%H%M%S)"
      c "$it 有本地实体 -> 备份到 $(basename "$bak") 后替换为软链"
      mv "$local_p" "$bak"
    fi
    mkdir -p "$(dirname "$local_p")"
    ln -s "$cloud_p" "$local_p"
    ok "$it -> 软链 OK"
  done
  ok "LINK 完成。注意：agent.db 仍为本机独立，不跨机。"
  exit 0
fi

if [ "$MODE" = "unlink" ]; then
  c "UNLINK: 解除软链，从 OneDrive 拷回实体（安全回退）"
  for it in "${ITEMS[@]}"; do
    local_p="$MEM/$it"; cloud_p="$CLOUD_MEM/$it"
    if [ ! -L "$local_p" ]; then sk "$it 非软链，跳过"; continue; fi
    rm "$local_p"
    if [ -e "$cloud_p" ]; then cp -R "$cloud_p" "$local_p"; ok "$it 已拷回实体"; else mkdir -p "$local_p"; ok "$it 云端空，建空目录"; fi
  done
  ok "UNLINK 完成"
  exit 0
fi

echo "未知模式: $MODE（用 link|status|unlink）" >&2; exit 1
