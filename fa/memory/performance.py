"""客观表现追踪 — 论点 vs 大盘的超额收益 + 客观评分.

Phase 1 的核心：用市场反馈给论点打分，防止 LLM 自我循环。

工作流:
  1. 论点保存时记录基线 (store._capture_baseline)
  2. review 时 PerformanceTracker.evaluate() 拉当前价 vs 当前指数
  3. 计算超额收益 + 客观得分 (objective_score)
  4. 写入 performance 表，供 dashboard 与进化引擎使用
"""

import json
from datetime import datetime
from typing import Optional

from .store import MemoryStore


# 超额收益 → 客观得分分档
# 设计理念: 跑赢/跑输 ±5pp 内是"持平"(0.5), 大幅跑赢/跑输才接近 1.0/0.0
SCORE_BANDS = [
    (15, 0.90),    # 超额 >15% → 0.90
    (5, 0.70),     # 5~15%  → 0.70
    (-5, 0.50),    # -5~5%  → 0.50 持平
    (-15, 0.30),   # -15~-5% → 0.30
    (-999, 0.10),  # < -15% → 0.10
]


def excess_to_score(excess_pct: Optional[float]) -> Optional[float]:
    """超额收益(%) → 客观得分 (0.0-1.0)。"""
    if excess_pct is None:
        return None
    for threshold, score in SCORE_BANDS:
        if excess_pct >= threshold:
            return score
    return 0.10


def verdict_label(excess_pct: Optional[float]) -> str:
    """超额收益 → 文字判定。"""
    if excess_pct is None:
        return "无法评估"
    if excess_pct >= 15:
        return "大幅跑赢"
    if excess_pct >= 5:
        return "跑赢"
    if excess_pct >= -5:
        return "持平"
    if excess_pct >= -15:
        return "跑输"
    return "大幅跑输"


class PerformanceTracker:
    """评估论点的客观表现 (vs 大盘基准)。"""

    def __init__(self, store: MemoryStore):
        self.store = store

    def evaluate(self, ticker: str, at_date: str = None,
                 subjective_score: float = None,
                 weight_objective: float = 0.7) -> Optional[dict]:
        """评估某只股票的论点表现并写入 performance 表。

        at_date: 评估时点（None=今天）
        subjective_score: 主观得分（预测验证准确率 0-1），用于综合
        weight_objective: 客观得分权重（默认 0.7，参考 PDF 设计 0.7×obj + 0.3×llm）

        返回评估结果 dict，失败返回 None。
        """
        thesis = self.store.get_thesis(ticker)
        if not thesis:
            return None
        if not thesis.get("baseline_price") or not thesis.get("baseline_index"):
            return {"error": "论点缺少基线（baseline_price/baseline_index 为空）",
                    "ticker": ticker,
                    "hint": "运行 store.backfill_baseline() 补录"}

        # 延迟 import
        from ..tools.data import fetch_price_at, fetch_index_at

        market = thesis.get("market") or "US"
        price = fetch_price_at(ticker, at_date)
        idx = fetch_index_at(market, at_date)
        if not price or not idx:
            return {"error": "无法获取当前价/指数", "ticker": ticker}

        # 收益率
        base_price = thesis["baseline_price"]
        base_idx = thesis["baseline_index"]
        stock_ret = round((price["close"] - base_price) / base_price * 100, 2)
        index_ret = round((idx["close"] - base_idx) / base_idx * 100, 2)
        excess = round(stock_ret - index_ret, 2)

        # 持仓天数
        days_held = 0
        try:
            d0 = datetime.strptime(thesis["baseline_date"], "%Y-%m-%d")
            d1 = datetime.strptime(price["date"], "%Y-%m-%d")
            days_held = (d1 - d0).days
        except Exception:
            pass

        # 评分
        obj_score = excess_to_score(excess)
        composite = None
        if obj_score is not None and subjective_score is not None:
            composite = round(weight_objective * obj_score +
                              (1 - weight_objective) * subjective_score, 3)
        elif obj_score is not None:
            composite = obj_score

        result = {
            "ticker": ticker,
            "thesis_id": thesis["id"],
            "checkpoint_date": price["date"],
            "days_held": days_held,
            "current_price": price["close"],
            "current_index": idx["close"],
            "stock_return": stock_ret,
            "index_return": index_ret,
            "excess_return": excess,
            "verdict": verdict_label(excess),
            "objective_score": obj_score,
            "subjective_score": subjective_score,
            "composite_score": composite,
            "baseline": {
                "date": thesis["baseline_date"],
                "price": base_price,
                "index": base_idx,
                "index_name": thesis["baseline_index_name"],
            },
        }

        # 写入 performance 表，返回 perf_id 供后续 critic 更新
        with self.store._conn() as c:
            c.execute("""
                INSERT INTO performance (thesis_id, ticker, checkpoint_date, days_held,
                    current_price, current_index, stock_return, index_return,
                    excess_return, objective_score, subjective_score, composite_score)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (thesis["id"], ticker, price["date"], days_held,
                  price["close"], idx["close"], stock_ret, index_ret, excess,
                  obj_score, subjective_score, composite))
            result["performance_id"] = c.execute("SELECT last_insert_rowid()").fetchone()[0]

        return result

    def attach_critic(self, performance_id: int, critic_result: dict):
        """把 Critic 评审结果回填到对应的 performance 行。"""
        with self.store._conn() as c:
            c.execute("""
                UPDATE performance SET
                    critic_score=?, raw_llm_score=?, final_score=?,
                    what_worked=?, what_failed=?, improvement_hints=?, critique=?
                WHERE id=?
            """, (
                critic_result.get("critic_score"),
                critic_result.get("raw_llm_score"),
                critic_result.get("final_score"),
                critic_result.get("what_worked", ""),
                critic_result.get("what_failed", ""),
                json.dumps(critic_result.get("improvement_hints", []), ensure_ascii=False),
                critic_result.get("critique", ""),
                performance_id,
            ))

    def get_history(self, ticker: str = None) -> list[dict]:
        """读取 performance 历史记录。"""
        with self.store._conn() as c:
            if ticker:
                rows = c.execute(
                    "SELECT * FROM performance WHERE ticker=? ORDER BY checked_at DESC",
                    (ticker,)
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM performance ORDER BY checked_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

    def summary(self) -> dict:
        """组合层面汇总：客观胜率、平均超额、最佳/最差。"""
        with self.store._conn() as c:
            # 每个论点取最新一次评估
            rows = c.execute("""
                SELECT p.* FROM performance p
                INNER JOIN (
                    SELECT thesis_id, MAX(checked_at) AS mx
                    FROM performance GROUP BY thesis_id
                ) latest ON p.thesis_id = latest.thesis_id AND p.checked_at = latest.mx
            """).fetchall()

        if not rows:
            return {"total": 0, "win_rate": None, "avg_excess": None,
                    "best": None, "worst": None}

        rows = [dict(r) for r in rows]
        wins = sum(1 for r in rows if (r["excess_return"] or 0) > 0)
        avg_ex = round(sum(r["excess_return"] or 0 for r in rows) / len(rows), 2)
        best = max(rows, key=lambda r: r["excess_return"] or -999)
        worst = min(rows, key=lambda r: r["excess_return"] or 999)
        avg_score = round(sum(r["objective_score"] or 0 for r in rows) / len(rows), 3)

        return {
            "total": len(rows),
            "wins": wins,
            "win_rate": round(wins / len(rows) * 100, 1),
            "avg_excess": avg_ex,
            "avg_objective_score": avg_score,
            "best": {"ticker": best["ticker"], "excess": best["excess_return"]},
            "worst": {"ticker": worst["ticker"], "excess": worst["excess_return"]},
        }
