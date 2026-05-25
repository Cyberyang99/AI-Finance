"""CoT Voter — 多专家联合投票/加权选股.

PDF1 §3.2-3.4 设计:
- 等权投票: 每条 CoT 一票，达到阈值 (≥3票) 纳入持仓
- 加权投票: 按 CoT 历史 IR 分位数赋权
- 阈值有效性区间: 阈值过低 → 无区分；过高 → 截面股票不足

由于我们尚无 IR 历史数据（需要 P2.5 回测基建），暂用 signal (1-10) 做权重的简化版。
"""

from typing import Optional

from .scorer import CotScorer, MATCH_VALUE


def vote(cot_scores: list[dict], min_votes: int = 3,
         min_confidence: int = 60) -> dict:
    """等权联合投票。

    cot_scores: list[CotScorer.score() 输出]
    一条 CoT 投"完全符合" or ("较符合" 且 confidence >= min_confidence) → 算 1 票
    "不符合"或低置信"较符合" → 0 票

    返回 {votes: int, decision: 持仓/观察/剔除, breakdown}
    """
    votes = 0
    voters = []
    for s in cot_scores:
        if s["match"] == "完全符合":
            votes += 1
            voters.append({"id": s["_cot_id"], "trigger": s["_trigger"], "match": "完全符合"})
        elif s["match"] == "较符合" and s["confidence"] >= min_confidence:
            votes += 1
            voters.append({"id": s["_cot_id"], "trigger": s["_trigger"], "match": "较符合"})

    if votes >= min_votes:
        decision = "纳入持仓"
    elif votes >= max(1, min_votes - 1):
        decision = "观察"
    else:
        decision = "剔除"

    return {
        "votes": votes,
        "total_cots": len(cot_scores),
        "decision": decision,
        "min_votes": min_votes,
        "voters": voters,
    }


def weighted_vote(cot_scores: list[dict], min_score: float = 3.5) -> dict:
    """加权综合分（按 signal 强度 + match level）。

    每条 CoT 贡献: match_value * (signal/10)
    - 完全符合 * 9/10 = 0.9
    - 较符合 * 9/10 = 0.45
    - 不符合 = 0

    PDF1 用的是 IR 分位数权重，我们暂用 signal 替代直到回测基建上线。
    """
    total_score = 0.0
    contributors = []
    for s in cot_scores:
        try:
            sig = int(s["_signal"])
        except (ValueError, TypeError):
            sig = 5
        weight = sig / 10.0
        mv = s.get("match_value", MATCH_VALUE.get(s.get("match", "不符合"), 0))
        # 置信度作为额外乘子（低置信度的判断打折）
        conf_factor = (s["confidence"] / 100.0) if s.get("match") != "不符合" else 1.0
        contrib = mv * weight * conf_factor
        total_score += contrib
        if contrib > 0:
            contributors.append({
                "id": s["_cot_id"],
                "trigger": s["_trigger"],
                "match": s["match"],
                "signal": sig,
                "confidence": s["confidence"],
                "contribution": round(contrib, 3),
            })

    return {
        "total_score": round(total_score, 3),
        "total_cots": len(cot_scores),
        "min_score": min_score,
        "decision": "纳入持仓" if total_score >= min_score else
                    ("观察" if total_score >= min_score * 0.7 else "剔除"),
        "contributors": sorted(contributors, key=lambda x: -x["contribution"]),
    }


def score_all_cots(cots: list[dict], stock_data: dict, news: Optional[str] = None,
                   scorer: Optional[CotScorer] = None,
                   progress_callback=None) -> list[dict]:
    """对一只股票，跑所有 CoT 的逐条评分。

    返回 cot_scores 列表（CotScorer.score 输出的合集）。
    progress_callback(i, n, current_score) 可选回调打印进度。
    """
    if scorer is None:
        scorer = CotScorer()

    out = []
    n = len(cots)
    for i, c in enumerate(cots, 1):
        s = scorer.score(c, stock_data, news=news)
        out.append(s)
        if progress_callback:
            progress_callback(i, n, s)
    return out
